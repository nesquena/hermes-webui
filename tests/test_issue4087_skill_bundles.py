"""Regression coverage for issue #4087, WebUI skill bundle slash parity."""

from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType

import pytest

import api.commands as commands


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _install_fake_skill_bundles(monkeypatch, *, bundles=None, resolver=None, builder=None):
    import sys

    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    agent_pkg.__path__ = []
    skill_bundles = ModuleType("agent.skill_bundles")
    skill_bundles.list_bundles = lambda: list(bundles or [])
    skill_bundles.resolve_bundle_command_key = resolver or (lambda name: None)
    skill_bundles.build_bundle_invocation_message = builder or (lambda key, args: None)
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_bundles", skill_bundles)


def test_bundle_routes_are_wired_through_dedicated_endpoints():
    assert 'if parsed.path == "/api/commands/bundles":' in ROUTES_PY
    assert 'if parsed.path == "/api/commands/bundles/resolve":' in ROUTES_PY
    assert 'return j(handler, {"bundles": list_command_bundles()})' in ROUTES_PY
    assert 'return j(handler, resolve_bundle_command(command))' in ROUTES_PY


def test_frontend_bundle_dispatch_uses_dedicated_metadata_and_resolve_calls():
    assert "api('/api/commands/bundles')" in COMMANDS_JS
    assert "api('/api/commands/bundles/resolve'" in COMMANDS_JS
    assert "const _bundleCmd=!_agentCmd&&typeof getBundleCommandMetadata==='function'" in MESSAGES_JS
    assert "await resolveBundleCommand(text,_bundleCmd)" in MESSAGES_JS


def test_frontend_checks_agent_ownership_before_bundle_resolution():
    agent_idx = MESSAGES_JS.find("await getAgentCommandMetadata(_parsedCmd.name)")
    bundle_idx = MESSAGES_JS.find("await getBundleCommandMetadata(_parsedCmd.name)")
    assert agent_idx != -1
    assert bundle_idx != -1
    assert agent_idx < bundle_idx


def test_list_command_bundles_returns_bundle_metadata(monkeypatch):
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    _install_fake_skill_bundles(
        monkeypatch,
        bundles=[
            {
                "slug": "incident-review",
                "description": "Investigate incidents with the bundled workflow",
                "skills": ["triage", "report"],
            },
            {
                "slug": "",
                "description": "ignored",
                "skills": ["missing-slug"],
            },
        ],
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    assert commands.list_command_bundles() == [
        {
            "name": "incident-review",
            "description": "Investigate incidents with the bundled workflow",
            "skill_count": 2,
            "source": "bundle",
        }
    ]
    assert seen == {"purpose": "/api/commands/bundles"}


def test_resolve_bundle_command_uses_bundle_runtime(monkeypatch):
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    def _resolve(name):
        seen["resolve_name"] = name
        return "/incident-review" if name == "incident-review" else None

    def _build(key, args):
        seen["build"] = (key, args)
        return ("$incident review the primary alerts", ["triage", "report"], [])

    _install_fake_skill_bundles(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    assert commands.resolve_bundle_command("/incident-review the primary alerts") == {
        "name": "incident-review",
        "source": "bundle",
        "message": "$incident review the primary alerts",
        "loaded_skills": ["triage", "report"],
        "missing_skills": [],
    }
    assert seen == {
        "purpose": "/api/commands/bundles/resolve",
        "resolve_name": "incident-review",
        "build": ("/incident-review", "the primary alerts"),
    }


def test_resolve_bundle_command_raises_for_unknown_bundle(monkeypatch):
    _install_fake_skill_bundles(monkeypatch)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    with pytest.raises(KeyError):
        commands.resolve_bundle_command("/does-not-exist investigate this")
