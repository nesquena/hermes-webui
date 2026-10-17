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


# ── Raw write snapshots (#8129 gate findings) ──────────────────────────────────


def _seed(cfg_path, mutate):
    from api import config as _config

    raw = _raw(cfg_path)
    mutate(raw)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()
    _config.reload_config()


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


def test_recursive_yaml_elsewhere_does_not_break_a_raw_write(cfg_path):
    """An unrelated recursive alias must not make the raw snapshot copy
    (_detached_copy) recurse forever."""
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


# ── An unchanged ${VAR} whose variable is EMPTY survives a save ────────────────
#
# A reference whose variable is set to "" is displayed as "" and re-submitted
# as "". That is an unchanged field, so the reference must stay on disk; it
# must not be removed or replaced by "".

EMPTY_REF = "${HERMES_TEST_EMPTY}"


def _aux_row(task: str) -> dict:
    from api import config

    return next(row for row in config.get_auxiliary_models()["tasks"] if row["task"] == task)


def _advanced_as_displayed(shown: dict, *, main: bool) -> dict:
    """The body the Settings advanced dialog posts back for unchanged fields."""
    advanced = {
        "base_url": shown.get("base_url") or "",
        "extra_body": shown.get("extra_body") or {},
        "api_key": "",
        "api_key_clear": False,
    }
    if main:
        advanced["service_tier"] = ""
    else:
        for field in ("timeout", "download_timeout", "max_concurrency"):
            value = shown.get(field)
            advanced[field] = "" if value is None else str(value)
    return advanced


def test_unchanged_empty_env_ref_survives_dashboard_save(cfg_path, monkeypatch):
    from api import config, dashboard_probe

    monkeypatch.setenv("HERMES_TEST_EMPTY", "")
    _seed(cfg_path, lambda raw: raw["webui"]["dashboard"].__setitem__("url", EMPTY_REF))

    shown = dashboard_probe.get_dashboard_config(config._load_yaml_config_file(cfg_path))
    assert shown["url"] == ""
    handler = _post("/api/dashboard/config", {"enabled": "always", "url": shown["url"]})
    assert handler.status == 200, handler.json_body()

    dashboard = _raw(cfg_path)["webui"]["dashboard"]
    assert dashboard["enabled"] == "always"
    assert dashboard.get("url") == EMPTY_REF, "the unchanged empty ${VAR} url was dropped"
    _assert_refs_survive(cfg_path, "POST /api/dashboard/config (empty ref)")


def test_unchanged_empty_env_ref_survives_auxiliary_advanced_save(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_EMPTY", "")

    def mutate(raw):
        raw["auxiliary"] = {
            "vision": {
                "provider": "openai",
                "model": "gpt-4o",
                "base_url": EMPTY_REF,
                "timeout": 30,
                # Greptile P2 on #8129: an optional extra_body field.
                "extra_body": {"optional": EMPTY_REF, "fixed": 1},
            }
        }

    _seed(cfg_path, mutate)
    row = _aux_row("vision")
    assert row["base_url"] == ""
    assert row["extra_body"] == {"optional": "", "fixed": 1}
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": row["provider"],
            "model": row["model"],
            "advanced": _advanced_as_displayed(row, main=False),
        },
    )
    assert handler.status == 200, handler.json_body()

    slot = _raw(cfg_path)["auxiliary"]["vision"]
    assert slot.get("base_url") == EMPTY_REF, "the unchanged empty ${VAR} base_url was dropped"
    assert slot.get("extra_body") == {"optional": EMPTY_REF, "fixed": 1}, "the empty ${VAR} in extra_body was dropped"
    assert slot["timeout"] == 30
    _assert_refs_survive(cfg_path, "POST /api/model/set auxiliary advanced (empty ref)")


def test_auxiliary_save_keeps_env_backed_provider_and_model(cfg_path, monkeypatch):
    """Greptile P2 on #8129: the advanced dialog re-posts the slot's displayed
    (resolved, stripped) provider/model; the references must stay."""
    monkeypatch.setenv("HERMES_TEST_AUX_PROVIDER", "openai")
    monkeypatch.setenv("HERMES_TEST_AUX_MODEL", " gpt-4o-mini ")

    def mutate(raw):
        raw["auxiliary"] = {
            "vision": {"provider": "${HERMES_TEST_AUX_PROVIDER}", "model": "${HERMES_TEST_AUX_MODEL}"}
        }

    _seed(cfg_path, mutate)
    row = _aux_row("vision")
    assert (row["provider"], row["model"]) == ("openai", "gpt-4o-mini")
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": row["provider"],
            "model": row["model"],
            "advanced": _advanced_as_displayed(row, main=False),
        },
    )
    assert handler.status == 200, handler.json_body()
    slot = _raw(cfg_path)["auxiliary"]["vision"]
    assert slot["provider"] == "${HERMES_TEST_AUX_PROVIDER}"
    assert slot["model"] == "${HERMES_TEST_AUX_MODEL}"

    # A model the user really picked is written as given.
    config_handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "openai", "model": "gpt-4o"},
    )
    assert config_handler.status == 200, config_handler.json_body()
    slot = _raw(cfg_path)["auxiliary"]["vision"]
    assert slot["provider"] == "${HERMES_TEST_AUX_PROVIDER}"
    assert slot["model"] == "gpt-4o"


def test_unchanged_empty_env_ref_survives_main_advanced_save(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_EMPTY", "")
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("base_url", EMPTY_REF))

    from api import config

    shown = config.get_auxiliary_models()["main"]
    assert shown["base_url"] == ""
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": shown["provider"],
            "model": shown["model"],
            "advanced": _advanced_as_displayed(shown, main=True),
        },
    )
    assert handler.status == 200, handler.json_body()

    model = _raw(cfg_path)["model"]
    assert model.get("base_url") == EMPTY_REF, "the unchanged empty ${VAR} base_url was dropped"
    _assert_refs_survive(cfg_path, "POST /api/model/set main advanced (empty ref)")


def test_preserve_env_ref_keeps_an_empty_reference_for_an_empty_submission(monkeypatch):
    from api import config

    monkeypatch.setenv("HERMES_TEST_EMPTY", "")
    assert config._preserve_env_ref(EMPTY_REF, "") == EMPTY_REF
    # A real change, or a reference that does not expand to "", is still written.
    assert config._preserve_env_ref(EMPTY_REF, "https://typed.example.test") == "https://typed.example.test"
    monkeypatch.setenv("HERMES_TEST_EMPTY", "https://set.example.test")
    assert config._preserve_env_ref(EMPTY_REF, "") == ""


# ── Synthetic secret end to end through every converted writer ───────────────

SYNTH_SECRET = "sk-synthetic-123"
SYNTH_URL = "https://llm.example.test/v1?key=" + SYNTH_SECRET

SYNTH_SEED = """\
model:
  default: synthetic-model
  provider: custom
  base_url: ${SYNTH_URL}
  api_key: ${SYNTH_KEY}
  extra_body:
    headers:
      Authorization: Bearer ${SYNTH_KEY}
providers:
  openai:
    api_key: ${SYNTH_KEY}
auxiliary:
  vision:
    provider: openai
    model: gpt-4o
    api_key: ${SYNTH_KEY}
    extra_body:
      headers:
        X-Key: ${SYNTH_KEY}
mcp_servers:
  bus:
    url: https://mcp.example.test/sse
    headers:
      Authorization: Bearer ${SYNTH_KEY}
      X-Alt: ${env:SYNTH_KEY}
    args:
      - --token
      - ${SYNTH_KEY}
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


def _assert_synth_refs_survive(path: Path, writer: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert SYNTH_SECRET not in text, f"{writer} wrote the synthetic secret into config.yaml"
    raw = _raw(path)
    expected = {
        "model.api_key": (raw["model"].get("api_key"), "${SYNTH_KEY}"),
        "model.base_url": (raw["model"].get("base_url"), "${SYNTH_URL}"),
        "model.extra_body": (
            raw["model"].get("extra_body"),
            {"headers": {"Authorization": "Bearer ${SYNTH_KEY}"}},
        ),
        "providers.openai.api_key": (raw["providers"]["openai"]["api_key"], "${SYNTH_KEY}"),
        "auxiliary.vision.api_key": (raw["auxiliary"]["vision"].get("api_key"), "${SYNTH_KEY}"),
        "auxiliary.vision.extra_body": (
            raw["auxiliary"]["vision"].get("extra_body"),
            {"headers": {"X-Key": "${SYNTH_KEY}"}},
        ),
        "mcp.bus.headers": (
            raw["mcp_servers"]["bus"]["headers"],
            {"Authorization": "Bearer ${SYNTH_KEY}", "X-Alt": "${env:SYNTH_KEY}"},
        ),
        "mcp.bus.args": (raw["mcp_servers"]["bus"]["args"], ["--token", "${SYNTH_KEY}"]),
    }
    for label, (actual, want) in expected.items():
        assert actual == want, f"{writer} lost the ${{...}} reference at {label}: {actual!r}"


def test_synthetic_secret_never_reaches_disk_through_any_converted_writer(
    cfg_path, skill_exists, monkeypatch
):
    from api import config, dashboard_probe, kanban_bridge
    from api.commands import execute_agent_command

    monkeypatch.setenv("SYNTH_KEY", SYNTH_SECRET)
    monkeypatch.setenv("SYNTH_URL", SYNTH_URL)
    cfg_path.write_text(SYNTH_SEED, encoding="utf-8")
    _reset_config_caches()
    config.reload_config()
    # The reader really resolves the secret, so a writer that saved an expanded
    # view would leak it.
    assert config._load_yaml_config_file(cfg_path)["model"]["api_key"] == SYNTH_SECRET

    def check(writer, handler=None):
        if handler is not None:
            assert handler.status == 200, (writer, handler.json_body())
        _assert_synth_refs_survive(cfg_path, writer)

    check("POST /api/reasoning display", _post("/api/reasoning", {"display": "show"}))
    assert _raw(cfg_path)["display"]["show_reasoning"] is True
    check("POST /api/reasoning effort", _post("/api/reasoning", {"effort": "high"}))
    assert _raw(cfg_path)["agent"]["reasoning_effort"] == "high"

    check(
        "POST /api/default-model",
        _post("/api/default-model", {"model": "synthetic-model-2", "provider": "custom"}),
    )
    assert _raw(cfg_path)["model"]["default"] == "synthetic-model-2"

    # Settings advanced dialogs post back what they displayed (env-resolved).
    shown_main = config.get_auxiliary_models()["main"]
    assert SYNTH_SECRET in json.dumps(shown_main["extra_body"])
    assert SYNTH_SECRET in shown_main["base_url"]
    check(
        "POST /api/model/set main advanced",
        _post(
            "/api/model/set",
            {
                "scope": "main",
                "provider": shown_main["provider"],
                "model": shown_main["model"],
                "advanced": _advanced_as_displayed(shown_main, main=True),
            },
        ),
    )

    check(
        "POST /api/model/set auxiliary",
        _post(
            "/api/model/set",
            {"scope": "auxiliary", "task": "vision", "provider": "openai", "model": "gpt-4o-mini"},
        ),
    )
    assert _raw(cfg_path)["auxiliary"]["vision"]["model"] == "gpt-4o-mini"
    row = _aux_row("vision")
    assert SYNTH_SECRET in json.dumps(row["extra_body"])
    advanced = _advanced_as_displayed(row, main=False)
    advanced["timeout"] = "45"
    check(
        "POST /api/model/set auxiliary advanced",
        _post(
            "/api/model/set",
            {
                "scope": "auxiliary",
                "task": "vision",
                "provider": row["provider"],
                "model": row["model"],
                "advanced": advanced,
            },
        ),
    )
    assert _raw(cfg_path)["auxiliary"]["vision"]["timeout"] == 45

    check(
        "POST /api/dashboard/config",
        _post("/api/dashboard/config", {"enabled": "always", "url": ""}),
    )
    assert _raw(cfg_path)["webui"]["dashboard"]["enabled"] == "always"
    assert dashboard_probe.get_dashboard_config(config._load_yaml_config_file(cfg_path))["enabled"] == "always"

    monkeypatch.setattr(kanban_bridge, "_config_payload", lambda *a, **k: {})
    kanban_bridge._update_config_payload({"lane_by_profile": True})
    check("kanban_bridge._update_config_payload")
    assert _raw(cfg_path)["dashboard"]["kanban"]["lane_by_profile"] is True

    _install_fake_codex_runtime_switch(monkeypatch)
    monkeypatch.setattr(config, "get_config", lambda: config._load_yaml_config_file(cfg_path))
    assert "codex_app_server" in execute_agent_command("/codex-runtime on")
    check("/codex-runtime")
    assert _raw(cfg_path)["model"]["openai_runtime"] == "codex_app_server"

    check("POST /api/skills/toggle disable", _toggle("demo", False))
    assert _raw(cfg_path)["skills"]["disabled"] == ["some-skill", "demo"]
    check("POST /api/skills/toggle enable", _toggle("demo", True))
    assert _raw(cfg_path)["skills"]["disabled"] == ["some-skill"]


def test_named_custom_auxiliary_endpoint_that_expands_empty_keeps_the_slot_url(cfg_path, monkeypatch):
    """Codex r3 on #8129: selecting a named custom provider whose base_url is a
    ${VAR} set to "" must not replace the slot's working URL with that empty
    reference. Master judged the EXPANDED value (empty -> no URL from the
    entry); the raw write must make the same decision."""
    monkeypatch.setenv("HERMES_TEST_EMPTY", "")

    def mutate(raw):
        raw["custom_providers"].append({"name": "Empty Endpoint", "base_url": EMPTY_REF})
        raw["auxiliary"] = {
            "vision": {
                "provider": "openai",
                "model": "gpt-4o",
                "base_url": "https://working.example.test/v1",
            }
        }

    _seed(cfg_path, mutate)
    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "custom:empty-endpoint", "model": "synthetic-model"},
    )
    assert handler.status == 200, handler.json_body()
    slot = _raw(cfg_path)["auxiliary"]["vision"]
    assert slot["provider"] == "custom:empty-endpoint"
    assert slot.get("base_url") == "https://working.example.test/v1", slot
    assert _raw(cfg_path)["custom_providers"][-1]["base_url"] == EMPTY_REF
    _assert_refs_survive(cfg_path, "POST /api/model/set auxiliary (empty named endpoint)")


def test_named_custom_auxiliary_endpoint_reference_is_not_expanded(cfg_path, monkeypatch):
    """The auxiliary writer reads the selected custom_providers entry from the
    raw document, so a ${VAR} endpoint is written as that reference."""
    monkeypatch.setenv("HERMES_TEST_NAMED_URL", "https://llm.example.test/v1?key=" + SECRET)
    _seed(
        cfg_path,
        lambda raw: raw["custom_providers"].append(
            {"name": "Secret Endpoint", "base_url": "${HERMES_TEST_NAMED_URL}"}
        ),
    )
    handler = _post(
        "/api/model/set",
        {"scope": "auxiliary", "task": "vision", "provider": "custom:secret-endpoint", "model": "synthetic-model"},
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["auxiliary"]["vision"]["base_url"] == "${HERMES_TEST_NAMED_URL}"
    _assert_refs_survive(cfg_path, "POST /api/model/set auxiliary (named ${VAR} endpoint)")


# ── Readers of the raw document must resolve ${VAR} where they used to rely on
#    Settings saves having expanded it to disk (Codex r4 on #8129) ─────────────


def _named_profile_tree(tmp_path, monkeypatch, *, profile_env: str):
    import api.profiles as profiles

    default_home = tmp_path / ".hermes"
    target_home = default_home / "profiles" / "writer"
    target_home.mkdir(parents=True)
    (target_home / "config.yaml").write_text(
        "model:\n  default: ${GATE_MODEL}\n  provider: ${GATE_PROVIDER}\n",
        encoding="utf-8",
    )
    (target_home / ".env").write_text(profile_env, encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "default"}, {"name": "writer"}])
    profiles._tls.profile = None
    return profiles


def test_per_client_profile_switch_returns_resolved_model_defaults(tmp_path, monkeypatch):
    """A kept ``${VAR}`` default must reach New Chat as the profile's value,
    not the literal placeholder."""
    profiles = _named_profile_tree(
        tmp_path, monkeypatch, profile_env="GATE_MODEL=gpt-4o\nGATE_PROVIDER=openai\n"
    )
    try:
        result = profiles.switch_profile("writer", process_wide=False)
    finally:
        profiles._tls.profile = None
    assert result["default_model"] == "gpt-4o"
    assert result["default_model_provider"] == "openai"


def test_per_client_profile_switch_never_returns_an_unresolved_placeholder(tmp_path, monkeypatch):
    """The target profile's env, not the server process env, resolves its
    defaults; an unresolvable reference is absent rather than a literal."""
    monkeypatch.setenv("GATE_MODEL", "process-only-model")
    monkeypatch.setenv("GATE_PROVIDER", "process-only-provider")
    profiles = _named_profile_tree(tmp_path, monkeypatch, profile_env="UNRELATED=1\n")
    try:
        result = profiles.switch_profile("writer", process_wide=False)
    finally:
        profiles._tls.profile = None
    assert result["default_model"] is None
    assert result["default_model_provider"] is None


def _launch_env(monkeypatch, **values):
    """Root ``${VAR}``s resolve against the launch-time env snapshot (plus root's
    .env), never the live process env a named-profile worker mirrors into."""
    from types import MappingProxyType

    import api.paths as paths

    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({**paths.STARTUP_ENV, **values}))


def test_remove_provider_key_matches_an_env_backed_active_provider(cfg_path, monkeypatch):
    """``model.provider: ${VAR}`` resolving to the removed provider must still
    drop ``model.api_key``; the raw provider reference itself survives."""
    from api import providers

    _launch_env(monkeypatch, GATE_PROVIDER="openai")
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("provider", "${GATE_PROVIDER}"))
    providers._clean_provider_key_from_config("openai")
    model = _raw(cfg_path)["model"]
    assert "api_key" not in model
    assert model["provider"] == "${GATE_PROVIDER}"
    _assert_no_secret(cfg_path, "_clean_provider_key_from_config (env-backed provider)")


def test_remove_provider_key_matches_an_env_backed_custom_provider_name(cfg_path, monkeypatch):
    from api import providers

    _launch_env(monkeypatch, GATE_CP_NAME="Secret Endpoint")
    _seed(cfg_path, lambda raw: raw["custom_providers"].append(
        {"name": "${GATE_CP_NAME}", "base_url": "https://llm.example.test/v1", "api_key": REF}
    ))
    providers._clean_provider_key_from_config("custom:secret-endpoint")
    entry = _raw(cfg_path)["custom_providers"][-1]
    assert "api_key" not in entry
    assert entry["name"] == "${GATE_CP_NAME}"
    _assert_no_secret(cfg_path, "_clean_provider_key_from_config (env-backed custom name)")
