"""Actual HTTP handler outcomes for fine-grained model/settings/skill bounds."""
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import config, routes
from api.governance import enforce, loader, resource_scope


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = {"email": "qa-resource@example.test", "groups": [], "method": "qa"}
    monkeypatch.setattr(enforce, "_request_identity", lambda _h: identity)
    monkeypatch.setattr("api.bot_builder.guard_profile_request", lambda *_a: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(routes.governance_api, "handle_governance_api", lambda *_a: False)
    monkeypatch.setattr(routes, "_check_csrf", lambda *_a: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(config, "resolve_model_provider", lambda model: (str(model).removeprefix("@qa:"), "qa", None))
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"default": "allowed-model", "provider": "qa"}})
    responses = []
    def respond(_handler, payload, status=200, **_kwargs):
        responses.append((status, payload))
        return True
    monkeypatch.setattr(routes, "j", respond)
    monkeypatch.setattr("api.helpers.j", respond)
    monkeypatch.setattr(routes, "bad", lambda h, message, status=400: respond(h, {"error": message}, status))
    def policy(deny=None, grants=None, managed=False, mode="enforce", access_mode="blacklist"):
        base = {"permissions": ["*"], "profiles": ["*"], "routes": ["*"],
                "skills": {"view": ["*"], "load": ["*"], "manage": ["*"]},
                "settings": {"read": ["*"], "write": ["*"]},
                "models": {"providers": ["*"], "models": ["*"]}}
        raw = {"mode": mode, "roles": {"member": {"grants": base}},
               "users": {identity["email"]: {"roles": ["member"], "deny": deny or {}}}}
        if grants is not None:
            raw["roles"]["member"]["grants"] = grants
        if managed:
            raw["users"][identity["email"]].update(access_mode=access_mode, access_level="elevated")
        parsed = loader.parse_governance_policy(raw)
        monkeypatch.setattr(loader, "get_policy", lambda: parsed)
    def request(path, body=None):
        responses.clear()
        handler = SimpleNamespace(path=path, command="GET" if body is None else "POST", headers={}, client_address=("127.0.0.1", 1))
        if body is None:
            routes.handle_get(handler, urlparse(path))
        else:
            monkeypatch.setattr(routes, "read_body", lambda _h: deepcopy(body))
            routes.handle_post(handler, urlparse(path))
        assert responses, "real route must return an HTTP response"
        return responses[-1]
    policy()
    return SimpleNamespace(policy=policy, request=request, handler=SimpleNamespace(), responses=responses)


@pytest.mark.parametrize("path", ["/api/skills/content?name=qa-forbidden", "/api/skills/content?name=qa-forbidden&file=private.txt"])
def test_denied_skill_content_never_reaches_file_reader(harness, monkeypatch, path):
    harness.policy(deny={"skills": {"view": ["qa-forbidden"]}})
    monkeypatch.setattr(routes, "_skill_view_from_active_dir", lambda *_: pytest.fail("denied file reader was reached"))
    assert harness.request(path)[0] == 403


@pytest.mark.parametrize("path", ["/api/skills/save", "/api/skills/delete", "/api/skills/toggle"])
def test_denied_skill_mutation_never_reaches_sink(harness, monkeypatch, path):
    harness.policy(deny={"skills": {"manage": ["qa-forbidden"]}})
    monkeypatch.setattr(routes, "_handle_skill_" + path.rsplit("/", 1)[-1], lambda *_: pytest.fail("denied skill mutation executed"))
    status, _ = harness.request(path, {"name": "QA Forbidden", "content": "changed", "enabled": True})
    assert status == 403


def test_skill_catalog_filters_denied_names_without_mutating_source(harness, monkeypatch):
    harness.policy(deny={"skills": {"view": ["qa-private*"]}})
    source = {"skills": [{"name": "qa-public", "description": "public"}, {"name": "qa-private", "description": "PRIVATE_MARKER"}]}
    original = deepcopy(source)
    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda *_a, **_k: source)
    status, payload = harness.request("/api/skills")
    assert status == 200
    assert [s["name"] for s in payload["skills"]] == ["qa-public"]
    assert source == original


def test_usage_does_not_leak_hidden_skill_names_or_totals(harness, monkeypatch):
    harness.policy(deny={"skills": {"view": ["qa-private"]}})
    monkeypatch.setattr("api.skill_usage.read_skill_usage", lambda *_: {"qa-public": {"use_count": 2}, "qa-private": {"use_count": 90}})
    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda *_a, **_k: {"skills": [{"name": "qa-public"}, {"name": "qa-private"}]})
    status, payload = harness.request("/api/skills/usage")
    assert status == 200
    assert payload["skill_names"] == ["qa-public"]
    assert list(payload["usage"]) == ["qa-public"]
    assert payload["total_invocations"] == 2
    assert payload["unique_skills_used"] == 1


@pytest.mark.parametrize("denied", ["theme", "appearance", "*"])
def test_appearance_selfservice_cannot_override_explicit_setting_denial(harness, monkeypatch, denied):
    harness.policy(deny={"settings": {"write": [denied]}})
    monkeypatch.setattr("api.settings_scope.settings_write_denial_for", lambda *_: pytest.fail("denied write passed resource boundary"))
    assert harness.request("/api/settings", {"theme": "light"})[0] == 403


def test_settings_read_filters_a_denied_group(harness, monkeypatch):
    harness.policy(deny={"settings": {"read": ["appearance"]}})
    monkeypatch.setattr(routes, "load_settings", lambda: {"theme": "dark", "skin": "private-skin", "bot_name": "Visible"})
    status, payload = harness.request("/api/settings")
    assert status == 200
    assert "theme" not in payload and "skin" not in payload
    assert payload["bot_name"] == "Visible"


@pytest.mark.parametrize("path,body", [
    ("/api/default-model", {"model": "denied-model", "provider": "qa"}),
    ("/api/model/set", {"scope": "main", "model": "denied-model", "provider": "qa"}),
    ("/api/model/set", {"scope": "auxiliary", "task": "title", "model": "@qa:denied-model", "provider": "qa"}),
])
def test_denied_model_cannot_be_saved_through_alternate_set_routes(harness, monkeypatch, path, body):
    harness.policy(deny={"models": {"models": ["denied-model"]}})
    monkeypatch.setattr(routes, "set_hermes_default_model", lambda *_a, **_k: pytest.fail("denied model was persisted"))
    monkeypatch.setattr(config, "set_auxiliary_model", lambda *_a, **_k: pytest.fail("denied auxiliary model was persisted"))
    assert harness.request(path, body)[0] == 403


@pytest.mark.parametrize("path,body", [("/api/models/live?provider=qa", None), ("/api/models/live", None), ("/api/models/refresh", {"provider": "qa"})])
def test_denied_provider_is_not_probed_or_refreshed(harness, monkeypatch, path, body):
    harness.policy(deny={"models": {"providers": ["qa"]}})
    monkeypatch.setattr(routes, "_handle_live_models", lambda *_: pytest.fail("denied provider was probed"))
    monkeypatch.setattr(config, "invalidate_provider_models_cache", lambda *_: pytest.fail("denied provider was refreshed"))
    assert harness.request(path, body)[0] == 403


@pytest.mark.parametrize("suffix", ["", "?freshness=session_visit"])
def test_model_catalog_hides_denied_defaults_aliases_badges_and_cached_entries(harness, monkeypatch, suffix):
    harness.policy(deny={"models": {"models": ["denied-model"], "providers": ["private-provider"]}})
    source = {"active_provider": "qa", "default_model": "denied-model", "aliases": {"secret": "denied-model", "ok": "allowed-model"},
              "configured_model_badges": {"denied-model": [{"label": "Private"}], "allowed-model": [{"label": "Allowed"}]},
              "groups": [{"provider_id": "qa", "models": [{"id": "allowed-model"}, {"id": "denied-model"}]},
                         {"provider_id": "private-provider", "models": [{"id": "private-model"}]}]}
    original = deepcopy(source)
    monkeypatch.setattr(routes, "get_available_models", lambda: source)
    monkeypatch.setattr(routes, "get_available_models_for_session_visit", lambda: source)
    status, payload = harness.request("/api/models" + suffix)
    assert status == 200
    assert payload["groups"] == [{"provider_id": "qa", "models": [{"id": "allowed-model"}]}]
    assert "default_model" not in payload
    assert payload["aliases"] == {"ok": "allowed-model"}
    assert list(payload["configured_model_badges"]) == ["allowed-model"]
    assert source == original


def test_auxiliary_catalog_filters_nested_denied_assignments(harness, monkeypatch):
    harness.policy(deny={"models": {"models": ["denied-model"]}})
    monkeypatch.setattr(config, "get_auxiliary_models", lambda: {"main": {"model": "denied-model", "provider": "qa"},
        "tasks": [{"task": "hidden", "model": "denied-model", "provider": "qa"}, {"task": "title", "model": "allowed-model", "provider": "qa"}]})
    status, payload = harness.request("/api/model/auxiliary")
    assert status == 200 and "main" not in payload
    assert [task["task"] for task in payload["tasks"]] == ["title"]


def test_omitted_setting_dimensions_follow_legacy_blacklist_and_whitelist_contracts(harness):
    grants = {"permissions": ["config:read"], "routes": ["*"]}
    harness.policy(grants=grants)
    assert resource_scope.setting_allowed(resource_scope.access_for(harness.handler), "theme", True)
    harness.policy(grants=grants, managed=True)
    assert resource_scope.setting_allowed(resource_scope.access_for(harness.handler), "theme", True)
    harness.policy(grants=grants, managed=True, deny={"settings": {"write": ["theme"]}})
    assert not resource_scope.setting_allowed(resource_scope.access_for(harness.handler), "theme", True)
    harness.policy(grants=grants, managed=True, access_mode="whitelist")
    assert not resource_scope.setting_allowed(resource_scope.access_for(harness.handler), "theme", True)


def test_report_only_keeps_resource_payloads_visible(harness, monkeypatch):
    harness.policy(deny={"skills": {"view": ["*"]}}, mode="report_only")
    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda *_a, **_k: {"skills": [{"name": "qa-public"}]})
    assert harness.request("/api/skills")[1]["skills"] == [{"name": "qa-public"}]


def test_unreadable_policy_denies_mutation(harness, monkeypatch):
    monkeypatch.setattr(loader, "get_policy", lambda: (_ for _ in ()).throw(RuntimeError("unreadable fixture policy")))
    status, payload = harness.request("/api/settings", {"theme": "light"})
    assert status == 403 and payload["reason"] == "policy_error"
