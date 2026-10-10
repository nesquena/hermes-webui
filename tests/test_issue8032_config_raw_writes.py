"""Behavioural regression tests for #8032 / #8115: config writers must not bake
env-expanded secrets into config.yaml.

Every WebUI path that persists config.yaml is driven through its real function
or HTTP route against a temp config.yaml that keeps a synthetic secret behind
``${VAR}`` / ``${env:VAR}`` references (top-level, nested in maps and nested in
lists). After each write the RAW file must still hold every reference and must
never contain the synthetic values, while the writer's own change lands.

Also covers the two #7854 gate blockers for the skills toggle (a scalar
``${VAR}`` that expands to a list; whitespace in an env-backed name) and the
#8115 cases (dashboard writer, nested references, intentional edit/delete).
"""

import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlparse

import pytest
import yaml

SECRET = "sk-test-SYNTHETIC-123"
TOKEN = "tok-test-SYNTHETIC-456"
REF = "${HERMES_TEST_SECRET}"
ENV_REF = "${env:HERMES_TEST_SECRET}"
BEARER_REF = "Bearer ${HERMES_TEST_TOKEN}"

SEED = f"""\
model:
  default: gpt-4o
  provider: openai
  api_key: {REF}
providers:
  openai:
    api_key: {REF}
mcp_servers:
  bus:
    url: https://mcp.example.test/sse
    headers:
      Authorization: {BEARER_REF}
      X-Alt: {ENV_REF}
  local:
    command: synthetic-mcp
    args:
      - --token
      - ${{HERMES_TEST_TOKEN}}
    env:
      API_KEY: {REF}
custom_providers:
  - name: synthetic
    base_url: https://llm.example.test/v1
    api_key: {REF}
display:
  show_reasoning: false
skills:
  disabled:
    - some-skill
dashboard:
  kanban:
    lane_by_profile: false
webui:
  dashboard:
    enabled: auto
"""


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _assert_no_secret(path: Path, writer: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text, f"{writer} wrote the expanded secret into config.yaml"
    assert TOKEN not in text, f"{writer} wrote the expanded token into config.yaml"


def _assert_refs_survive(path: Path, writer: str, *, skip=()) -> None:
    """Every seeded reference the writer did not intentionally change is still raw."""
    _assert_no_secret(path, writer)
    raw = _raw(path)
    checks = {
        "model.api_key": lambda r: r["model"]["api_key"] == REF,
        "providers.openai.api_key": lambda r: r["providers"]["openai"]["api_key"] == REF,
        "mcp.bus.Authorization": lambda r: r["mcp_servers"]["bus"]["headers"]["Authorization"] == BEARER_REF,
        "mcp.bus.X-Alt": lambda r: r["mcp_servers"]["bus"]["headers"]["X-Alt"] == ENV_REF,
        "mcp.local.args": lambda r: r["mcp_servers"]["local"]["args"] == ["--token", "${HERMES_TEST_TOKEN}"],
        "mcp.local.env": lambda r: r["mcp_servers"]["local"]["env"]["API_KEY"] == REF,
        "custom_providers[0].api_key": lambda r: r["custom_providers"][0]["api_key"] == REF,
    }
    for label, check in checks.items():
        if label in skip:
            continue
        assert check(raw), f"{writer} lost the ${{...}} reference at {label}: {raw!r}"


class _FakeHandler:
    """Minimal request handler for routes.handle_post / direct handlers."""

    def __init__(self, body: dict | None = None):
        body_bytes = json.dumps(body or {}).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _post(path: str, body: dict) -> _FakeHandler:
    from api.routes import handle_post

    handler = _FakeHandler(body)
    handle_post(handler, urlparse(f"http://127.0.0.1{path}"))
    return handler


def _reset_config_caches():
    from api import config

    with config._cfg_lock:
        config._cfg_cache.clear()
        config.cfg = config._cfg_cache
        config._cfg_fingerprint = None
        config._cfg_mtime = 0.0
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    import api.config as config
    import api.routes as routes

    path = tmp_path / "config.yaml"
    path.write_text(SEED, encoding="utf-8")
    monkeypatch.setenv("HERMES_TEST_SECRET", SECRET)
    monkeypatch.setenv("HERMES_TEST_TOKEN", TOKEN)

    def _test_config_path():
        return path

    monkeypatch.setattr(config, "_get_config_path", _test_config_path)
    # routes imported the resolver by name; a non-api.config resolver also
    # makes _active_profile_config_path() (skills) follow it.
    monkeypatch.setattr(routes, "_get_config_path", _test_config_path)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    _reset_config_caches()
    yield path
    _reset_config_caches()


@pytest.fixture
def skill_exists(monkeypatch, tmp_path):
    import api.routes as routes

    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda d: [d])
    monkeypatch.setattr(routes, "_find_skill_in_dirs", lambda name, dirs: (skill_dir, skill_md))


def _set_skills(path: Path, skills: dict) -> None:
    raw = _raw(path)
    raw["skills"] = skills
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _toggle(name: str, enabled: bool) -> _FakeHandler:
    return _post("/api/skills/toggle", {"name": name, "enabled": enabled})


def _reader_disabled() -> set:
    import api.routes as routes

    return routes._get_disabled_skill_names_for_profile()


# ── The shared save helper (#8032 minimal repro) ───────────────────────────────


def test_expanded_load_then_save_keeps_references(cfg_path):
    from api import config

    data = config._load_yaml_config_file(cfg_path)
    assert data["providers"]["openai"]["api_key"] == SECRET  # reads expand
    data["display"]["show_reasoning"] = True
    config._save_yaml_config_file(cfg_path, data)

    _assert_refs_survive(cfg_path, "_save_yaml_config_file(expanded)")
    assert _raw(cfg_path)["display"]["show_reasoning"] is True


def test_save_helper_writes_an_intentionally_changed_value(cfg_path):
    from api import config

    data = config._load_yaml_config_file(cfg_path)
    data["providers"]["openai"]["api_key"] = "sk-user-typed-new-value"
    del data["custom_providers"][0]["api_key"]
    config._save_yaml_config_file(cfg_path, data)

    raw = _raw(cfg_path)
    assert raw["providers"]["openai"]["api_key"] == "sk-user-typed-new-value"
    assert "api_key" not in raw["custom_providers"][0]
    _assert_refs_survive(
        cfg_path,
        "_save_yaml_config_file(edit)",
        skip=("providers.openai.api_key", "custom_providers[0].api_key"),
    )


def test_save_helper_restores_refs_in_shortened_and_reordered_lists(cfg_path):
    from api import config

    raw = _raw(cfg_path)
    raw["custom_providers"].insert(0, {"name": "first", "api_key": "${HERMES_TEST_TOKEN}"})
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    data = config._load_yaml_config_file(cfg_path)
    data["mcp_servers"]["local"]["args"] = data["mcp_servers"]["local"]["args"][1:]
    data["custom_providers"].reverse()
    config._save_yaml_config_file(cfg_path, data)

    _assert_no_secret(cfg_path, "_save_yaml_config_file(shifted lists)")
    raw = _raw(cfg_path)
    assert raw["mcp_servers"]["local"]["args"] == ["${HERMES_TEST_TOKEN}"]
    assert [cp["name"] for cp in raw["custom_providers"]] == ["synthetic", "first"]
    assert raw["custom_providers"][0]["api_key"] == REF
    assert raw["custom_providers"][1]["api_key"] == "${HERMES_TEST_TOKEN}"


# ── api/config.py writers via their HTTP routes ────────────────────────────────


def test_reasoning_display_route_keeps_references(cfg_path):
    handler = _post("/api/reasoning", {"display": "show"})
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/reasoning display")
    assert _raw(cfg_path)["display"]["show_reasoning"] is True


def test_reasoning_effort_route_keeps_references(cfg_path):
    handler = _post("/api/reasoning", {"effort": "high"})
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/reasoning effort")
    assert _raw(cfg_path)["agent"]["reasoning_effort"] == "high"


def test_max_tokens_keeps_references(cfg_path):
    from api import config

    config.set_max_tokens(2048)
    _assert_refs_survive(cfg_path, "set_max_tokens")
    assert _raw(cfg_path)["max_tokens"] == 2048


def test_default_model_route_keeps_references(cfg_path):
    handler = _post("/api/default-model", {"model": "gpt-4o-mini", "provider": "openai"})
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/default-model")
    assert _raw(cfg_path)["model"]["default"] == "gpt-4o-mini"


def test_default_model_env_backed_provider_is_not_a_provider_switch(cfg_path, monkeypatch):
    from api import config

    monkeypatch.setenv("HERMES_TEST_PROVIDER", "openai")
    raw = _raw(cfg_path)
    raw["model"]["provider"] = "${HERMES_TEST_PROVIDER}"
    raw["model"]["base_url"] = "https://kept.example.test/v1"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert config.set_hermes_default_model("gpt-4o", provider="openai")["ok"] is True
    model = _raw(cfg_path)["model"]
    assert model["provider"] == "${HERMES_TEST_PROVIDER}"
    assert model["base_url"] == "https://kept.example.test/v1"
    _assert_refs_survive(cfg_path, "set_hermes_default_model (env provider)")


def test_default_model_intentional_api_key_edit_and_clear(cfg_path):
    """#8115: the user's explicit new value is written; clearing removes it."""
    from api import config

    config.set_hermes_default_model(
        "gpt-4o", provider="openai", advanced={"api_key": "sk-user-typed-new-value"}
    )
    assert _raw(cfg_path)["model"]["api_key"] == "sk-user-typed-new-value"
    _assert_refs_survive(cfg_path, "set_hermes_default_model (edit)", skip=("model.api_key",))

    config.set_hermes_default_model("gpt-4o", provider="openai", advanced={"api_key_clear": True})
    assert "api_key" not in _raw(cfg_path)["model"]
    _assert_refs_survive(cfg_path, "set_hermes_default_model (clear)", skip=("model.api_key",))


def test_auxiliary_model_route_keeps_references(cfg_path):
    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/model/set auxiliary")
    assert _raw(cfg_path)["auxiliary"]["vision"]["model"] == "gpt-4o-mini"


def test_auxiliary_model_matches_env_backed_custom_provider_name(cfg_path, monkeypatch):
    from api import config

    monkeypatch.setenv("HERMES_TEST_CP_NAME", "My Server")
    raw = _raw(cfg_path)
    raw["custom_providers"].append(
        {"name": "${HERMES_TEST_CP_NAME}", "base_url": "https://correct.example.test/v1"}
    )
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    config.set_auxiliary_model("vision", "custom:my-server", "some-model")
    raw = _raw(cfg_path)
    assert raw["auxiliary"]["vision"]["base_url"] == "https://correct.example.test/v1"
    assert raw["custom_providers"][1]["name"] == "${HERMES_TEST_CP_NAME}"
    _assert_refs_survive(cfg_path, "set_auxiliary_model (env-backed custom name)")


# ── Writers outside api/config.py ──────────────────────────────────────────────


def test_dashboard_route_keeps_references(cfg_path):
    """#8115 reproducer: Settings -> System dashboard link save."""
    handler = _post("/api/dashboard/config", {"enabled": "always", "url": ""})
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/dashboard/config")
    assert _raw(cfg_path)["webui"]["dashboard"]["enabled"] == "always"


def test_dashboard_env_backed_url_kept_changed_or_cleared(cfg_path, monkeypatch):
    from api import config, dashboard_probe

    monkeypatch.setenv("HERMES_TEST_DASH_URL", "https://dash.example.test/hermes")
    raw = _raw(cfg_path)
    raw["webui"]["dashboard"]["url"] = "${HERMES_TEST_DASH_URL}"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    # Re-saving the URL the panel displayed (resolved) keeps the reference.
    shown = dashboard_probe.get_dashboard_config(config._load_yaml_config_file(cfg_path))["url"]
    assert shown.startswith("https://dash.example.test/hermes")
    dashboard_probe.save_dashboard_config({"enabled": "always", "url": shown})
    assert _raw(cfg_path)["webui"]["dashboard"]["url"] == "${HERMES_TEST_DASH_URL}"

    # An intentional edit is written as given ...
    dashboard_probe.save_dashboard_config({"enabled": "always", "url": "https://other.example.test"})
    assert _raw(cfg_path)["webui"]["dashboard"]["url"].startswith("https://other.example.test")

    # ... and clearing it removes the key.
    dashboard_probe.save_dashboard_config({"enabled": "auto", "url": ""})
    assert "url" not in _raw(cfg_path)["webui"]["dashboard"]
    _assert_refs_survive(cfg_path, "save_dashboard_config (url cases)")


def test_kanban_config_keeps_references(cfg_path, monkeypatch):
    from api import kanban_bridge

    monkeypatch.setattr(kanban_bridge, "_config_payload", lambda *a, **k: {})
    kanban_bridge._update_config_payload({"lane_by_profile": True})
    _assert_refs_survive(cfg_path, "kanban_bridge._update_config_payload")
    assert _raw(cfg_path)["dashboard"]["kanban"]["lane_by_profile"] is True


def _install_fake_codex_runtime_switch(monkeypatch):
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    switch = ModuleType("hermes_cli.codex_runtime_switch")

    def parse_args(arg_string):
        return ("codex_app_server", []) if arg_string == "on" else (None, [])

    def apply(config, new_value, *, persist_callback=None):
        # Same contract as hermes_cli.codex_runtime_switch.apply/set_runtime.
        if new_value is not None:
            if not isinstance(config.get("model"), dict):
                config["model"] = {}
            config["model"]["openai_runtime"] = new_value
            if persist_callback:
                persist_callback(config)
        return SimpleNamespace(success=True, message=f"openai_runtime: {new_value}")

    switch.parse_args = parse_args
    switch.apply = apply
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.codex_runtime_switch", switch)


def test_codex_runtime_command_keeps_references(cfg_path, monkeypatch):
    from api import config
    from api.commands import execute_agent_command

    _install_fake_codex_runtime_switch(monkeypatch)
    # The runtime config is the env-EXPANDED cache, exactly what get_config() serves.
    monkeypatch.setattr(config, "get_config", lambda: config._load_yaml_config_file(cfg_path))

    assert "codex_app_server" in execute_agent_command("/codex-runtime on")
    _assert_refs_survive(cfg_path, "/codex-runtime")
    assert _raw(cfg_path)["model"]["openai_runtime"] == "codex_app_server"


def test_mcp_update_toggle_delete_keep_other_references(cfg_path):
    """MCP writers (#7822 transaction) keep untouched refs; an edit is written."""
    import api.routes as routes

    handler = _FakeHandler()
    routes._handle_mcp_server_update(
        handler,
        "bus",
        {"url": "https://mcp.example.test/sse", "headers": {"Authorization": "Bearer user-typed"}},
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["bus"]["headers"] == {"Authorization": "Bearer user-typed"}
    _assert_refs_survive(cfg_path, "MCP update", skip=("mcp.bus.Authorization", "mcp.bus.X-Alt"))

    handler = _FakeHandler()
    routes._handle_mcp_server_toggle(handler, "local", {"enabled": False})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["local"]["enabled"] is False
    _assert_refs_survive(cfg_path, "MCP toggle", skip=("mcp.bus.Authorization", "mcp.bus.X-Alt"))

    handler = _FakeHandler()
    routes._handle_mcp_server_delete(handler, "local")
    assert handler.status == 200, handler.json_body()
    assert "local" not in _raw(cfg_path)["mcp_servers"]
    _assert_refs_survive(
        cfg_path,
        "MCP delete",
        skip=("mcp.bus.Authorization", "mcp.bus.X-Alt", "mcp.local.args", "mcp.local.env"),
    )


# ── Skills toggle: raw transaction + the two #7854 gate blockers ───────────────


def test_skill_toggle_route_keeps_references(cfg_path, skill_exists):
    handler = _toggle("demo", False)
    assert handler.status == 200, handler.json_body()
    _assert_refs_survive(cfg_path, "POST /api/skills/toggle")
    assert _raw(cfg_path)["skills"]["disabled"] == ["some-skill", "demo"]
    assert _reader_disabled() == {"some-skill", "demo"}


def test_blocker1_scalar_ref_expanding_to_list_never_enables_another_skill(
    cfg_path, skill_exists, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_DISABLED", '["demo", "other"]')
    _set_skills(cfg_path, {"disabled": "${HERMES_TEST_DISABLED}"})
    assert _reader_disabled() == {"demo", "other"}

    handler = _toggle("demo", True)
    assert handler.status == 200, handler.json_body()
    # "other" must stay disabled: the remaining resolved names are explicit.
    assert _reader_disabled() == {"other"}, "enabling 'demo' silently enabled 'other'"
    assert _raw(cfg_path)["skills"]["disabled"] == ["other"]
    # ... and the response says the reference was replaced.
    assert handler.json_body()["env_refs_resolved"] == ["skills.disabled"]
    _assert_refs_survive(cfg_path, "skills toggle (blocker 1)")


def test_blocker1_disable_and_noop_with_list_valued_scalar_ref(cfg_path, skill_exists, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_DISABLED", '["demo", "other"]')
    _set_skills(
        cfg_path,
        {"disabled": "${HERMES_TEST_DISABLED}", "platform_disabled": {"webui": "${HERMES_TEST_DISABLED}"}},
    )

    # Disabling an already-disabled skill keeps the reference verbatim.
    handler = _toggle("demo", False)
    assert handler.status == 200, handler.json_body()
    skills = _raw(cfg_path)["skills"]
    assert skills["disabled"] == "${HERMES_TEST_DISABLED}"
    assert skills["platform_disabled"]["webui"] == "${HERMES_TEST_DISABLED}"
    assert "env_refs_resolved" not in handler.json_body()

    # Disabling a third skill is a partial edit: write explicit resolved names.
    handler = _toggle("third", False)
    assert handler.status == 200, handler.json_body()
    assert _reader_disabled() == {"demo", "other", "third"}
    skills = _raw(cfg_path)["skills"]
    assert skills["disabled"] == ["demo", "other", "third"]
    assert skills["platform_disabled"]["webui"] == ["demo", "other", "third"]
    assert handler.json_body()["env_refs_resolved"] == [
        "skills.disabled",
        "skills.platform_disabled.webui",
    ]
    _assert_refs_survive(cfg_path, "skills toggle (blocker 1 disable)")


def test_blocker2_whitespace_in_env_backed_name_enable_really_enables(
    cfg_path, skill_exists, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_DISABLED_ONE", " demo ")
    _set_skills(cfg_path, {"disabled": ["${HERMES_TEST_DISABLED_ONE}", "other"]})
    assert _reader_disabled() == {"demo", "other"}

    handler = _toggle("demo", True)
    assert handler.status == 200, handler.json_body()
    assert handler.json_body()["ok"] is True
    assert "demo" not in _reader_disabled(), "ok:true but the skill is still disabled"
    assert _raw(cfg_path)["skills"]["disabled"] == ["other"]
    _assert_refs_survive(cfg_path, "skills toggle (blocker 2 enable)")


def test_blocker2_whitespace_in_env_backed_name_disable_does_not_duplicate(
    cfg_path, skill_exists, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_DISABLED_ONE", " demo ")
    _set_skills(
        cfg_path,
        {
            "disabled": ["${HERMES_TEST_DISABLED_ONE}"],
            "platform_disabled": {"webui": "${HERMES_TEST_DISABLED_ONE}"},
        },
    )

    handler = _toggle("demo", False)
    assert handler.status == 200, handler.json_body()
    skills = _raw(cfg_path)["skills"]
    assert skills["disabled"] == ["${HERMES_TEST_DISABLED_ONE}"]
    assert skills["platform_disabled"]["webui"] == "${HERMES_TEST_DISABLED_ONE}"
    assert _reader_disabled() == {"demo"}


def test_scalar_single_name_ref_is_kept_when_another_skill_is_disabled(
    cfg_path, skill_exists, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_DISABLED_ONE", " other ")
    _set_skills(cfg_path, {"disabled": "${HERMES_TEST_DISABLED_ONE}"})

    handler = _toggle("demo", False)
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["skills"]["disabled"] == ["${HERMES_TEST_DISABLED_ONE}", "demo"]
    assert _reader_disabled() == {"other", "demo"}
    assert "env_refs_resolved" not in handler.json_body()


def test_skill_toggle_preserves_nested_env_refs_in_platform_list(cfg_path, skill_exists, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_DISABLED_ONE", "other")
    _set_skills(
        cfg_path,
        {
            "disabled": ["${HERMES_TEST_DISABLED_ONE}", "${env:HERMES_TEST_SECRET}"],
            "platform_disabled": {"webui": ["${HERMES_TEST_DISABLED_ONE}"], "telegram": [REF]},
        },
    )

    handler = _toggle("demo", False)
    assert handler.status == 200, handler.json_body()
    skills = _raw(cfg_path)["skills"]
    assert skills["disabled"] == ["${HERMES_TEST_DISABLED_ONE}", "${env:HERMES_TEST_SECRET}", "demo"]
    assert skills["platform_disabled"]["webui"] == ["${HERMES_TEST_DISABLED_ONE}", "demo"]
    assert skills["platform_disabled"]["telegram"] == [REF]
    _assert_refs_survive(cfg_path, "skills toggle (platform list)")


# ── Gate findings on #8129 (adversarial Codex, 2026-10-10) ─────────────


def test_raw_write_does_not_propagate_through_a_shared_yaml_anchor(cfg_path):
    """A raw write snapshot must detach YAML aliases: editing the vision slot
    must not also change a compression slot that shares its anchor (the
    env-expanding reader always produced detached copies)."""
    from api import config

    text = cfg_path.read_text(encoding="utf-8") + (
        "auxiliary:\n"
        "  vision: &shared\n"
        "    provider: openai\n"
        "    model: gpt-4o\n"
        "  compression: *shared\n"
    )
    cfg_path.write_text(text, encoding="utf-8")
    _reset_config_caches()

    config.set_auxiliary_model("vision", "openai", "gpt-4o-mini")
    raw = _raw(cfg_path)
    assert raw["auxiliary"]["vision"]["model"] == "gpt-4o-mini"
    assert raw["auxiliary"]["compression"]["model"] == "gpt-4o"
    _assert_refs_survive(cfg_path, "set_auxiliary_model (shared anchor)")


def test_explicit_clear_survives_when_the_reference_expands_to_empty(cfg_path, monkeypatch):
    """Clearing a field whose ${VAR} currently expands to "" must clear it, not
    put the reference back."""
    from api import config, dashboard_probe

    assert config._preserve_env_ref("${HERMES_TEST_EMPTY_URL}", "") == ""
    monkeypatch.setenv("HERMES_TEST_EMPTY_URL", "")
    raw = _raw(cfg_path)
    raw["webui"]["dashboard"]["url"] = "${HERMES_TEST_EMPTY_URL}"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()

    dashboard_probe.save_dashboard_config({"enabled": "auto", "url": ""})
    assert "url" not in _raw(cfg_path)["webui"]["dashboard"]
    _assert_refs_survive(cfg_path, "save_dashboard_config (clear empty-expanding ref)")


CP_URL_REF = "${HERMES_TEST_CP_URL}"
CP_URL = "https://llm.example.test/v1?key=" + SECRET


def test_named_custom_provider_url_is_written_as_its_template(cfg_path, monkeypatch):
    """Selecting a named custom provider whose base_url is an env reference to a
    secret-bearing URL must write that ${VAR} template into model.base_url,
    never the expanded URL (gate repro: POST /api/default-model)."""
    monkeypatch.setenv("HERMES_TEST_CP_URL", CP_URL)
    raw = _raw(cfg_path)
    raw["custom_providers"][0]["base_url"] = CP_URL_REF
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()
    # The provider resolver reads the in-memory config, so reload it from the
    # file (as the server does on its next stat check).
    from api import config as _config

    _config.reload_config()

    handler = _post(
        "/api/default-model",
        {"model": "@custom:synthetic:synthetic-model", "provider": "custom:synthetic"},
    )
    assert handler.status == 200, handler.json_body()
    raw = _raw(cfg_path)
    assert raw["model"].get("base_url") in (None, CP_URL_REF)
    assert raw["custom_providers"][0]["base_url"] == CP_URL_REF
    _assert_no_secret(cfg_path, "POST /api/default-model (named custom provider url)")


def test_unnamed_custom_auxiliary_url_is_written_as_its_template(cfg_path, monkeypatch):
    """An unnamed ``custom`` auxiliary slot derives its URL from the main model
    block; an env reference to a secret-bearing URL there must not be expanded
    into the slot (gate repro: POST /api/model/set auxiliary)."""
    monkeypatch.setenv("HERMES_TEST_CP_URL", CP_URL)
    raw = _raw(cfg_path)
    raw["model"]["provider"] = "custom"
    raw["model"]["base_url"] = CP_URL_REF
    raw["custom_providers"] = []
    raw.pop("auxiliary", None)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()
    # The provider resolver reads the in-memory config, so reload it from the
    # file (as the server does on its next stat check).
    from api import config as _config

    _config.reload_config()

    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "custom", "model": "synthetic-model"},
    )
    assert handler.status == 200, handler.json_body()
    raw = _raw(cfg_path)
    assert raw["auxiliary"]["vision"].get("base_url") in (None, CP_URL_REF)
    _assert_no_secret(cfg_path, "POST /api/model/set (unnamed custom auxiliary url)")


TOKEN_REF = "${HERMES_TEST_TOKEN}"


def test_reordered_same_length_scalar_list_keeps_its_template(cfg_path):
    """Same-length scalar lists match by value, not position: moving an
    env-backed arg must not bake the token (senior gate repro)."""
    from api import config

    out = config._restore_env_ref_templates({"args": [TOKEN, "--token"]}, {"args": ["--token", TOKEN_REF]})
    assert out == {"args": [TOKEN_REF, "--token"]}
    out3 = config._restore_env_ref_templates(
        {"args": ["--a", "--b", TOKEN]}, {"args": ["--a", TOKEN_REF, "--b"]}
    )
    assert out3 == {"args": ["--a", "--b", TOKEN_REF]}
    # A literal the user typed on disk is never swapped for a template.
    assert config._restore_env_ref_templates(
        {"args": ["plain-value", "--token"]}, {"args": ["--token", "plain-value"]}
    ) == {"args": ["plain-value", "--token"]}


def test_mcp_update_reordering_an_env_backed_arg_keeps_the_reference(cfg_path):
    """Through the real MCP update handler: reordering args that contain the
    expanded token keeps ``${HERMES_TEST_TOKEN}`` on disk."""
    import api.routes as routes

    raw = _raw(cfg_path)
    raw.setdefault("mcp_servers", {})["reorder"] = {"command": "synthetic-mcp", "args": ["--token", TOKEN_REF]}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "reorder", {"command": "synthetic-mcp", "args": [TOKEN, "--token"]})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["reorder"]["args"] == [TOKEN_REF, "--token"]
    _assert_no_secret(cfg_path, "MCP update (reordered args)")


# ── Gate round 2 findings on #8129 (adversarial Codex) ─────────────────

SAME_URL = "https://same.example.test/v1"


def _seed(cfg_path, mutate):
    from api import config as _config

    raw = _raw(cfg_path)
    mutate(raw)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()
    _config.reload_config()


def test_main_url_comes_from_the_selected_provider_not_an_equal_template(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_URL_A", SAME_URL)
    monkeypatch.setenv("HERMES_TEST_URL_B", SAME_URL)

    def mutate(raw):
        raw["custom_providers"] = [
            {"name": "first", "base_url": "${HERMES_TEST_URL_A}"},
            {"name": "second", "base_url": "${HERMES_TEST_URL_B}"},
        ]

    _seed(cfg_path, mutate)
    handler = _post("/api/default-model", {"model": "@custom:second:synthetic-model", "provider": "custom:second"})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["model"].get("base_url") in (None, "${HERMES_TEST_URL_B}")


def test_unnamed_aux_url_comes_from_the_main_block_not_an_equal_template(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_URL_A", SAME_URL)
    monkeypatch.setenv("HERMES_TEST_URL_B", SAME_URL)

    def mutate(raw):
        raw["model"] = {"default": "synthetic-model", "provider": "custom", "base_url": "${HERMES_TEST_URL_B}"}
        raw["custom_providers"] = [{"name": "unrelated", "base_url": "${HERMES_TEST_URL_A}"}]
        raw.pop("auxiliary", None)

    _seed(cfg_path, mutate)
    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "compression", "provider": "custom", "model": "synthetic-model"},
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["auxiliary"]["compression"].get("base_url") in (None, "${HERMES_TEST_URL_B}")


def test_literal_selected_endpoint_stays_literal(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_URL_A", SAME_URL)

    def mutate(raw):
        raw["custom_providers"] = [
            {"name": "unrelated", "base_url": "${HERMES_TEST_URL_A}"},
            {"name": "literal", "base_url": SAME_URL},
        ]

    _seed(cfg_path, mutate)
    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "custom:literal", "model": "synthetic-model"},
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["auxiliary"]["vision"].get("base_url") in (None, SAME_URL)


def test_keyed_provider_url_is_written_as_its_template(cfg_path, monkeypatch):
    """Selecting an env-backed ``providers.<id>.base_url`` (pre-existing gap)
    writes the template, never the expanded URL."""
    monkeypatch.setenv("HERMES_TEST_CP_URL", CP_URL)

    def mutate(raw):
        raw["providers"] = {"synthetic": {"base_url": CP_URL_REF, "models": ["synthetic-model"]}}

    _seed(cfg_path, mutate)
    handler = _post("/api/default-model", {"model": "synthetic-model", "provider": "synthetic"})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["model"].get("base_url") in (None, CP_URL_REF)
    _assert_no_secret(cfg_path, "POST /api/default-model (providers.<id> url)")


def test_list_restore_keeps_literals_and_uses_each_template_once(cfg_path):
    from api import config

    # A literal equal to the token's expansion and the reference trade places.
    assert config._restore_env_ref_templates(
        {"args": [TOKEN, TOKEN_REF, "--tail"]}, {"args": [TOKEN_REF, TOKEN, "--tail"]}
    ) == {"args": [TOKEN, TOKEN_REF, "--tail"]}
    # An unchanged, fully expanded save keeps the on-disk order and kinds.
    assert config._restore_env_ref_templates(
        {"args": [TOKEN, TOKEN]}, {"args": [TOKEN_REF, TOKEN]}
    ) == {"args": [TOKEN_REF, TOKEN]}
    # One template occurrence is never restored twice.
    assert config._restore_env_ref_templates({"args": [TOKEN, TOKEN]}, {"args": [TOKEN_REF]}) == {
        "args": [TOKEN_REF, TOKEN]
    }


def test_mcp_update_literal_and_reference_trade_places(cfg_path):
    import api.routes as routes

    def mutate(raw):
        raw.setdefault("mcp_servers", {})["reorder"] = {
            "command": "synthetic-mcp",
            "args": [TOKEN_REF, TOKEN, "--tail"],
        }

    _seed(cfg_path, mutate)
    handler = _FakeHandler()
    routes._handle_mcp_server_update(
        handler, "reorder", {"command": "synthetic-mcp", "args": [TOKEN, TOKEN_REF, "--tail"]}
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["reorder"]["args"] == [TOKEN, TOKEN_REF, "--tail"]


def test_recursive_yaml_elsewhere_does_not_break_a_save(cfg_path):
    """An unrelated recursive alias must not make the restore guard recurse
    forever (base returned 200)."""
    import api.routes as routes

    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + "metadata: &cycle\n  self: *cycle\n",
        encoding="utf-8",
    )
    _reset_config_caches()
    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "local", {"command": "synthetic-mcp", "args": ["--new"]})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["local"]["args"] == ["--new"]
    _assert_refs_survive(cfg_path, "MCP update (recursive YAML elsewhere)", skip=("mcp.local.args", "mcp.local.env"))

