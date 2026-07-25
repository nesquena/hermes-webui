"""Tests for the global toolset configuration bridge (api/toolset_config.py).

hermes_cli is a separate deployment from hermes-webui and is not installed
in this test venv, so every test injects a minimal fake hermes_cli.{tools_
config,config,platforms} tree (+ a top-level `toolsets` module) via
sys.modules — mirroring the pattern already used by
tests/test_provider_management.py and tests/test_commands_endpoint.py for
the same reason. Covers: graceful "unavailable" degradation, the read/write
split (list/config/models always readable, mutations gated behind
HERMES_WEBUI_ALLOW_TOOLSET_WRITE), and each of the four write endpoints
(toggle, provider select, model select, env save) including their
validation-error paths.
"""
import json
import sys
from io import BytesIO
from types import ModuleType
from urllib.parse import urlparse

import pytest

from api import toolset_config


def _install_fake_hermes_cli(monkeypatch, *, config=None, env=None):
    """Install a minimal fake hermes_cli package tree + `toolsets` module.

    Returns a dict of shared mutable state ("config", "env", "save_calls",
    "env_saves") so tests can assert what got written.
    """
    state = {
        "config": config if config is not None else {},
        "env": dict(env or {}),
        "save_calls": [],
        "env_saves": [],
    }

    hermes_cli_pkg = ModuleType("hermes_cli")
    hermes_cli_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)

    # ── hermes_cli.config ──
    cfg_mod = ModuleType("hermes_cli.config")
    cfg_mod.load_config = lambda: state["config"]

    def save_config(config, **kwargs):
        state["config"] = config
        state["save_calls"].append({k: v for k, v in config.items()})

    cfg_mod.save_config = save_config
    cfg_mod.get_env_value = lambda key: state["env"].get(key)

    def save_env_value(key, value):
        state["env"][key] = value
        state["env_saves"].append((key, value))

    cfg_mod.save_env_value = save_env_value
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg_mod)

    # ── hermes_cli.platforms ──
    platforms_mod = ModuleType("hermes_cli.platforms")
    platforms_mod.platform_label = lambda platform, default=None: (default or platform).upper()
    monkeypatch.setitem(sys.modules, "hermes_cli.platforms", platforms_mod)

    # ── toolsets (top-level) ──
    toolsets_mod = ModuleType("toolsets")
    toolsets_mod.resolve_toolset = lambda name: {
        "tts": ["speak"], "image_gen": ["generate_image"],
    }.get(name, [])
    monkeypatch.setitem(sys.modules, "toolsets", toolsets_mod)

    # ── hermes_cli.tools_config ──
    tc_mod = ModuleType("hermes_cli.tools_config")

    toolset_rows = [
        ("tts", "\U0001F50A Text-to-Speech", "Speak responses aloud"),
        ("image_gen", "\U0001F3A8 Image Generation", "Generate images"),
    ]
    tc_mod._get_effective_configurable_toolsets = lambda: list(toolset_rows)
    tc_mod._toolset_configuration_platform = lambda name, default="cli": default
    tc_mod._get_platform_tools = lambda config, platform, include_default_mcp_servers=False: set(
        config.get("platform_toolsets", {}).get(platform, [])
    )

    def _save_platform_tools(config, platform, enabled_keys):
        config.setdefault("platform_toolsets", {})[platform] = sorted(enabled_keys)
        save_config(config)

    tc_mod._save_platform_tools = _save_platform_tools

    def gui_toolset_label(label):
        parts = (label or "").split(None, 1)
        if len(parts) == 2 and parts[0] and not any(ch.isalnum() and ch.isascii() for ch in parts[0]):
            return parts[1]
        return label

    tc_mod.gui_toolset_label = gui_toolset_label
    tc_mod._toolset_has_keys = lambda name, config: False

    tool_categories = {
        "tts": {
            "name": "Text-to-Speech",
            "providers": [
                {"name": "Edge TTS", "tts_provider": "edge", "env_vars": []},
                {
                    "name": "OpenAI TTS",
                    "tts_provider": "openai",
                    "env_vars": [{"key": "VOICE_TOOLS_OPENAI_KEY", "prompt": "OpenAI API key"}],
                },
            ],
        },
        "image_gen": {
            "name": "Image Generation",
            "providers": [
                {
                    "name": "FAL",
                    "imagegen_backend": "fal",
                    # Two keys so a partial-update test can prove that saving
                    # one credential leaves the other one untouched.
                    "env_vars": [
                        {"key": "FAL_KEY", "prompt": "FAL key"},
                        {"key": "FAL_ORG_ID", "prompt": "FAL org id"},
                    ],
                },
            ],
        },
    }
    tc_mod.TOOL_CATEGORIES = tool_categories
    tc_mod._visible_providers = lambda cat, config, force_fresh=False: list(cat.get("providers", []))

    def _is_provider_active(provider, config, force_fresh=False):
        if provider.get("tts_provider"):
            return config.get("tts", {}).get("provider") == provider["tts_provider"]
        if provider.get("imagegen_backend"):
            img = config.get("image_gen", {}) or {}
            return img.get("provider") in (None, "", "fal")
        return False

    tc_mod._is_provider_active = _is_provider_active

    def apply_provider_selection(ts_key, provider_name, config):
        cat = tool_categories.get(ts_key)
        if cat is None:
            raise KeyError(f"Toolset has no configurable category: {ts_key}")
        provider = next((p for p in cat["providers"] if p["name"] == provider_name), None)
        if provider is None:
            raise KeyError(f"Unknown provider {provider_name!r} for toolset {ts_key!r}")
        if provider.get("tts_provider"):
            config.setdefault("tts", {})["provider"] = provider["tts_provider"]
        if provider.get("imagegen_backend"):
            config.setdefault("image_gen", {})["provider"] = "fal"

    tc_mod.apply_provider_selection = apply_provider_selection

    image_catalog = ({"flux-pro": {"display": "Flux Pro"}, "flux-dev": {"display": "Flux Dev"}}, "flux-dev")
    tc_mod._plugin_image_gen_catalog = lambda plugin_name: image_catalog if plugin_name == "fal" else ({}, None)
    tc_mod._plugin_video_gen_catalog = lambda plugin_name: ({}, None)

    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", tc_mod)

    return state


def _enable_write_gate(monkeypatch):
    monkeypatch.setenv(toolset_config._WRITE_GATE_ENV, "1")


def _disable_write_gate(monkeypatch):
    monkeypatch.delenv(toolset_config._WRITE_GATE_ENV, raising=False)


# ── Graceful degradation ────────────────────────────────────────────────


def test_list_toolsets_unavailable_without_hermes_cli(monkeypatch):
    # Some venvs (a dev checkout with hermes-agent on sys.path via conftest)
    # can import the real hermes_cli; bare CI venvs cannot. If it imports, we
    # block submodule resolution by emptying the real package's __path__
    # (same technique tests/test_commands_endpoint.py uses) and evict any
    # already-imported submodule from sys.modules; if it doesn't, the
    # "hermes-agent not installed" branch under test is already the natural
    # state and nothing needs patching.
    try:
        import hermes_cli
    except ModuleNotFoundError:
        pass
    else:
        monkeypatch.setattr(hermes_cli, "__path__", [], raising=False)
    for mod in ("hermes_cli.tools_config", "hermes_cli.config"):
        monkeypatch.delitem(sys.modules, mod, raising=False)

    with pytest.raises(toolset_config.ToolsetConfigUnavailable) as excinfo:
        toolset_config.list_toolsets()
    assert excinfo.value.status == 503


# ── GET list ──────────────────────────────────────────────────────────────


def test_list_toolsets_reports_labels_and_enabled_state(monkeypatch):
    _install_fake_hermes_cli(
        monkeypatch,
        config={"platform_toolsets": {"cli": ["tts"]}},
    )
    _disable_write_gate(monkeypatch)

    result = toolset_config.list_toolsets()

    assert result["allowed"] is False
    assert result["write_gate_env"] == toolset_config._WRITE_GATE_ENV
    by_name = {row["name"]: row for row in result["toolsets"]}
    assert by_name["tts"]["enabled"] is True
    assert by_name["tts"]["label"] == "Text-to-Speech"  # emoji stripped
    assert by_name["tts"]["tools"] == ["speak"]
    assert by_name["image_gen"]["enabled"] is False
    assert by_name["image_gen"]["platform"] == "cli"


def test_list_toolsets_reports_allowed_true_when_gate_enabled(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.list_toolsets()

    assert result["allowed"] is True


# ── PUT toggle ────────────────────────────────────────────────────────────


def test_toggle_toolset_disabled_without_write_gate(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _disable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.toggle_toolset("tts", True)
    assert excinfo.value.status == 403
    assert toolset_config._WRITE_GATE_ENV in str(excinfo.value)


def test_toggle_toolset_unknown_name_returns_400(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.toggle_toolset("not-a-real-toolset", True)
    assert excinfo.value.status == 400


def test_toggle_toolset_enables_and_persists(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.toggle_toolset("image_gen", True)

    assert result == {"ok": True, "name": "image_gen", "platform": "cli", "enabled": True}
    assert "image_gen" in state["config"]["platform_toolsets"]["cli"]
    assert state["save_calls"], "toggling must persist through save_config"


def test_toggle_toolset_disables_and_persists(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={"platform_toolsets": {"cli": ["tts", "image_gen"]}})
    _enable_write_gate(monkeypatch)

    toolset_config.toggle_toolset("tts", False)

    assert "tts" not in state["config"]["platform_toolsets"]["cli"]
    assert "image_gen" in state["config"]["platform_toolsets"]["cli"]


# ── GET toolset config (providers + key status) ─────────────────────────


def test_get_toolset_config_lists_providers_with_key_status(monkeypatch):
    _install_fake_hermes_cli(
        monkeypatch,
        config={"tts": {"provider": "edge"}},
        env={"VOICE_TOOLS_OPENAI_KEY": "sk-x"},
    )

    result = toolset_config.get_toolset_config("tts")

    assert result["has_category"] is True
    names = {p["name"] for p in result["providers"]}
    assert names == {"Edge TTS", "OpenAI TTS"}
    openai_row = next(p for p in result["providers"] if p["name"] == "OpenAI TTS")
    assert openai_row["env_vars"][0]["is_set"] is True
    assert result["active_provider"] == "Edge TTS"


def test_get_toolset_config_unknown_toolset_400(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.get_toolset_config("nope")
    assert excinfo.value.status == 400


# ── PUT provider ──────────────────────────────────────────────────────────


def test_select_toolset_provider_requires_write_gate(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _disable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.select_toolset_provider("tts", "OpenAI TTS")
    assert excinfo.value.status == 403


def test_select_toolset_provider_success(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.select_toolset_provider("tts", "OpenAI TTS")

    assert result == {"ok": True, "name": "tts", "provider": "OpenAI TTS"}
    assert state["config"]["tts"]["provider"] == "openai"


def test_select_toolset_provider_unknown_provider_400(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.select_toolset_provider("tts", "Does Not Exist")
    assert excinfo.value.status == 400


# ── GET / PUT models ─────────────────────────────────────────────────────


def test_get_toolset_models_toolset_without_catalog(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})

    result = toolset_config.get_toolset_models("tts")

    assert result == {"name": "tts", "has_models": False, "models": [], "current": None, "default": None}


def test_get_toolset_models_resolves_active_provider_catalog(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})

    result = toolset_config.get_toolset_models("image_gen")

    assert result["has_models"] is True
    assert result["provider"] == "FAL"
    assert result["current"] == "flux-dev"  # falls back to catalog default
    ids = {m["id"] for m in result["models"]}
    assert ids == {"flux-pro", "flux-dev"}


def test_select_toolset_model_requires_write_gate(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _disable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.select_toolset_model("image_gen", "flux-pro")
    assert excinfo.value.status == 403


def test_select_toolset_model_no_catalog_toolset_400(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.select_toolset_model("tts", "some-model")
    assert excinfo.value.status == 400


def test_select_toolset_model_unknown_model_400(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.select_toolset_model("image_gen", "not-a-model")
    assert excinfo.value.status == 400


def test_select_toolset_model_success_persists(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.select_toolset_model("image_gen", "flux-pro")

    assert result == {"ok": True, "name": "image_gen", "model": "flux-pro", "plugin": "fal"}
    assert state["config"]["image_gen"]["model"] == "flux-pro"


# ── PUT env ───────────────────────────────────────────────────────────────


def test_save_toolset_env_requires_write_gate(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _disable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.save_toolset_env("image_gen", {"FAL_KEY": "abc"})
    assert excinfo.value.status == 403


def test_save_toolset_env_rejects_non_dict(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.save_toolset_env("image_gen", "not-a-dict")
    assert excinfo.value.status == 400


def test_save_toolset_env_rejects_unknown_key(monkeypatch):
    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    with pytest.raises(toolset_config.ToolsetConfigError) as excinfo:
        toolset_config.save_toolset_env("image_gen", {"SOME_RANDOM_KEY": "abc"})
    assert excinfo.value.status == 400
    assert "SOME_RANDOM_KEY" in excinfo.value.extra.get("unknown_keys", [])


def test_save_toolset_env_saves_allowlisted_key(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.save_toolset_env("image_gen", {"FAL_KEY": "sk-real-value"})

    assert result["saved"] == ["FAL_KEY"]
    assert state["env"]["FAL_KEY"] == "sk-real-value"
    assert result["is_set"]["FAL_KEY"] is True


def test_save_toolset_env_blank_value_is_skipped_not_saved(monkeypatch):
    state = _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)

    result = toolset_config.save_toolset_env("image_gen", {"FAL_KEY": "   "})

    assert result["saved"] == []
    assert result["skipped"] == ["FAL_KEY"]
    assert "FAL_KEY" not in state["env"]


def test_save_toolset_env_blank_value_preserves_an_existing_secret(monkeypatch):
    """The actual invariant: a blank submit must not clear a stored key.

    The form round-trips a stored key as an empty input, so an ordinary save of
    an unrelated field submits `""` for every untouched credential. Asserting
    against an initially *empty* env only proves "blank writes nothing to an
    empty dict" — it cannot catch a regression that overwrites a set key.
    """
    state = _install_fake_hermes_cli(
        monkeypatch, config={}, env={"FAL_KEY": "sk-existing-secret"}
    )
    _enable_write_gate(monkeypatch)

    result = toolset_config.save_toolset_env("image_gen", {"FAL_KEY": "   "})

    assert result["saved"] == []
    assert result["skipped"] == ["FAL_KEY"]
    assert state["env"]["FAL_KEY"] == "sk-existing-secret"
    assert state["env_saves"] == [], "a blank submit reached the env writer"
    assert result["is_set"]["FAL_KEY"] is True


def test_save_toolset_env_empty_string_preserves_an_existing_secret(monkeypatch):
    """Same invariant for a literal empty string, not just whitespace."""
    state = _install_fake_hermes_cli(
        monkeypatch, config={}, env={"FAL_KEY": "sk-existing-secret"}
    )
    _enable_write_gate(monkeypatch)

    result = toolset_config.save_toolset_env("image_gen", {"FAL_KEY": ""})

    assert result["saved"] == []
    assert state["env"]["FAL_KEY"] == "sk-existing-secret"
    assert state["env_saves"] == []


def test_save_toolset_env_updates_one_key_without_touching_the_other(monkeypatch):
    """A partial update must rewrite only the field the user actually changed."""
    state = _install_fake_hermes_cli(
        monkeypatch,
        config={},
        env={"FAL_KEY": "sk-existing-secret", "FAL_ORG_ID": "org-existing"},
    )
    _enable_write_gate(monkeypatch)

    result = toolset_config.save_toolset_env(
        "image_gen", {"FAL_KEY": "sk-rotated", "FAL_ORG_ID": ""}
    )

    assert result["saved"] == ["FAL_KEY"]
    assert state["env"]["FAL_KEY"] == "sk-rotated"
    assert state["env"]["FAL_ORG_ID"] == "org-existing"
    assert [key for key, _ in state["env_saves"]] == ["FAL_KEY"]


# ── Route contract ──────────────────────────────────────────────────────────
# The `/api/tools/toolsets/` prefix is owned by this API. Before, an unmatched
# sub-path fell out of the block without returning, kept walking the remaining
# matchers, and ended at the generic "unhandled" signal — so a stale client, a
# typo'd JS call or a curl probe got an unpredictable reply instead of a 404.


class _RouteHandler:
    def __init__(self, path, body=None):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []
        self._body = body if body is not None else {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    def log_message(self, *args, **kwargs):
        pass


@pytest.mark.parametrize(
    "path",
    [
        "/api/tools/toolsets/image_gen",          # one segment, no action
        "/api/tools/toolsets/image_gen/bogus",    # unrecognised action
        "/api/tools/toolsets/image_gen/config/x",  # too many segments
        "/api/tools/toolsets/",                   # empty remainder
    ],
)
def test_get_unknown_toolset_subpath_returns_404_instead_of_falling_through(
    monkeypatch, path
):
    import api.routes as routes

    _install_fake_hermes_cli(monkeypatch, config={})
    handler = _RouteHandler(path)

    handled = routes.handle_get(handler, urlparse(path))

    assert handled is not False, f"{path} fell through the toolset block"
    assert handler.status == 404
    assert "error" in handler.response_json


def test_get_known_toolset_subpaths_still_dispatch(monkeypatch):
    """The 404 guard must not swallow the two real GET actions."""
    import api.routes as routes

    _install_fake_hermes_cli(monkeypatch, config={})

    for path in (
        "/api/tools/toolsets/image_gen/config",
        "/api/tools/toolsets/image_gen/models",
    ):
        handler = _RouteHandler(path)
        assert routes.handle_get(handler, urlparse(path)) is not False
        assert handler.status == 200, path


@pytest.mark.parametrize(
    "path",
    [
        "/api/tools/toolsets/image_gen/bogus",
        "/api/tools/toolsets/image_gen/config/x",
        "/api/tools/toolsets/",
    ],
)
def test_put_unknown_toolset_subpath_returns_404(monkeypatch, path):
    import api.routes as routes

    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {})
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility",
        lambda *a, **kw: True,
    )
    handler = _RouteHandler(path)

    handled = routes.handle_put(handler, urlparse(path))

    assert handled is not False
    assert handler.status == 404


def test_put_bare_toolsets_collection_is_not_claimed(monkeypatch):
    """`PUT /api/tools/toolsets` has no handler and must stay unclaimed.

    Honest scope: this is a GUARD, not a regression pin. The removed
    `or parsed.path == "/api/tools/toolsets"` clause was dead — with the bare
    path `parts` is `[]` either way, so old and new code both fall through to
    `return False`. Asserting on the response cannot distinguish them. What it
    CAN do is pin that the block never starts claiming the collection path, so
    the assertion below checks the block is not entered at all.
    """
    import api.routes as routes

    _install_fake_hermes_cli(monkeypatch, config={})
    _enable_write_gate(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {})
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility",
        lambda *a, **kw: True,
    )
    # If the block were entered, it would import the toolset writers. Make that
    # observable: this is the only assertion that actually discriminates.
    entered = []
    real_import = toolset_config.toggle_toolset
    monkeypatch.setattr(
        toolset_config, "toggle_toolset",
        lambda *a, **kw: entered.append(True) or real_import(*a, **kw),
    )

    path = "/api/tools/toolsets"
    handler = _RouteHandler(path)

    assert routes.handle_put(handler, urlparse(path)) is False
    assert handler.status is None
    assert entered == [], "the bare collection path reached a toolset writer"
