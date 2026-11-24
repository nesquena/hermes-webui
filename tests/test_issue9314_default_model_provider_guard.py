"""Regression coverage for #9314 gaps 1 and 2 (follow-ups to #9304).

Gap 1: a config whose ``model.provider`` already holds an undeclared
``<built-in>:<suffix>`` value fails every turn with the Agent's generic
``invalid_provider`` error and nothing told the user how to repair it. A save
that would keep that value must name it and point at Settings → Main model
options → Save instead of returning ``ok``.

Gap 2: ``POST /api/session/update`` and profile creation stored a picker group
id (``nous:meituan/longcat-2.5``) as sent. Both now use the same normalisation
and the same ``_provider_is_config_declared()`` guard as
``set_hermes_default_model()``: a declared colon name (``openrouter:eu``) keeps
being persisted verbatim, an undeclared picker pin drops to the built-in
provider.

Gap 3 (``google:`` pin dropping a configured ``base_url``) is out of scope and
stays on the issue.
"""
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
import yaml

import api.config as config
import api.profiles as profiles
import api.routes as routes
from api.config import set_hermes_default_model


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "model:\n  provider: nous\n  base_url: https://router.nous.ai/v1\n  default: anthropic/claude-opus-4.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: Path(str(config_file)))
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "provider": "nous",
                "base_url": "https://router.nous.ai/v1",
                "default": "anthropic/claude-opus-4.6",
            }
        }
    )
    try:
        config._cfg_mtime = config_file.stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0
    config._yaml_file_cache.clear()
    config.invalidate_models_cache()

    yield config_file

    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._available_models_cache = old_cache
    config._available_models_cache_ts = old_cache_ts
    config._yaml_file_cache.clear()


def _seed_config(config_file: Path, data: dict) -> dict:
    """Write *data* to the temp config and point both the raw and runtime view at it."""
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    config._yaml_file_cache.clear()
    config.cfg.clear()
    config.cfg.update(yaml.safe_load(config_file.read_text(encoding="utf-8")))
    config._cfg_mtime = config_file.stat().st_mtime
    return data


def _post(path: str, body: dict) -> tuple[dict, int]:
    """Run a POST route through the real handler with captured JSON output."""
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    def fake_bad(handler, msg, status=400):
        captured["payload"] = {"error": msg}
        captured["status"] = status
        return True

    with patch.object(routes, "_check_csrf", return_value=True), patch.object(
        routes, "read_body", return_value=body
    ), patch.object(routes, "j", side_effect=fake_j), patch.object(
        routes, "bad", side_effect=fake_bad
    ):
        handled = routes.handle_post(object(), type("P", (), {"path": path, "query": ""})())
    assert handled is True, f"{path} was not handled"
    return captured["payload"], captured["status"]


BROKEN = {"model": {"provider": "openrouter:eu", "default": "old-model"}}
# A healthy profile: the refusal cases below request a pin that is NOT the value
# already stored, which is the shape the maintainer kept as an error
# (2026-11-23T18:24:00Z). Re-saving the stored value itself must repair it — see
# tests/test_issue9314_regate_ownership.py.
HEALTHY = {"model": {"provider": "nous", "default": "old-model"}}


# ── Gap 1: a broken saved provider is named, not silently kept ──────────────

def test_save_names_an_undeclared_builtin_pin_and_points_to_main_model_options(_isolate_config):
    """``openrouter:eu`` with no declaration is not a picker pin: refuse and name it."""
    config_file = _isolate_config
    _seed_config(config_file, HEALTHY)

    with pytest.raises(ValueError) as excinfo:
        set_hermes_default_model("new-model", provider="openrouter:eu")

    message = str(excinfo.value)
    assert "openrouter:eu" in message, message
    assert "Main model options" in message, message
    assert "Save" in message, message
    # The error must not rewrite the config on its own.
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"
    assert saved["model"]["default"] == "old-model"


def test_save_without_provider_refuses_to_keep_a_broken_stored_provider(_isolate_config):
    """A save that carries no provider would persist the broken value unchanged."""
    config_file = _isolate_config
    _seed_config(config_file, BROKEN)

    with pytest.raises(ValueError) as excinfo:
        set_hermes_default_model("new-model")

    message = str(excinfo.value)
    assert "openrouter:eu" in message, message
    assert "Main model options" in message, message
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "openrouter:eu"
    assert saved["model"]["default"] == "old-model"


def test_default_model_route_returns_400_with_the_bad_value(_isolate_config):
    config_file = _isolate_config
    _seed_config(config_file, HEALTHY)

    payload, status = _post(
        "/api/default-model", {"model": "new-model", "provider": "openrouter:eu"}
    )

    assert status == 400, (status, payload)
    assert "openrouter:eu" in payload["error"], payload
    assert "Main model options" in payload["error"], payload
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"
    assert saved["model"]["default"] == "old-model"


def test_model_set_main_route_returns_400_with_the_bad_value(_isolate_config):
    config_file = _isolate_config
    _seed_config(config_file, HEALTHY)

    payload, status = _post(
        "/api/model/set",
        {"scope": "main", "provider": "openrouter:eu", "model": "new-model"},
    )

    assert status == 400, (status, payload)
    assert "openrouter:eu" in payload["error"], payload
    assert "Main model options" in payload["error"], payload
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"


def test_picker_pin_save_still_repairs_a_poisoned_provider(_isolate_config):
    """The shipped #9304 repair must keep working: the pin is the selected model."""
    config_file = _isolate_config
    _seed_config(
        config_file,
        {
            "model": {
                "provider": "nous:meituan/longcat-2.5",
                "base_url": "https://router.nous.ai/v1",
                "default": "meituan/longcat-2.5:free",
            }
        },
    )

    payload, status = _post(
        "/api/default-model",
        {"model": "meituan/longcat-2.5:free", "provider": "nous:meituan/longcat-2.5"},
    )

    assert status == 200, (status, payload)
    assert payload["provider"] == "nous", payload
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"


# ── Gap 1: a declared colon provider keeps its exact name ───────────────────

@pytest.mark.parametrize("section", ["providers", "custom_providers"])
def test_declared_colon_provider_is_still_accepted_verbatim(_isolate_config, section):
    config_file = _isolate_config
    entry = {"base_url": "https://eu-proxy.example/v1", "api_key": "DUMMY_EU"}
    declared = {"openrouter:eu": entry} if section == "providers" else [
        {"name": "openrouter:eu", **entry}
    ]
    _seed_config(
        config_file,
        {
            "model": {
                "provider": "openrouter:eu",
                "default": "gpt-x",
                "base_url": "https://eu-proxy.example/v1",
            },
            section: declared,
        },
    )

    payload, status = _post(
        "/api/default-model", {"model": "gpt-x", "provider": "openrouter:eu"}
    )

    assert status == 200, (status, payload)
    assert payload["provider"] == "openrouter:eu", payload
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "openrouter:eu"
    assert saved["model"]["base_url"] == "https://eu-proxy.example/v1"


def test_healthy_save_without_provider_still_works(_isolate_config):
    config_file = _isolate_config
    _seed_config(config_file, {"model": {"provider": "nous", "default": "old-model"}})

    result = set_hermes_default_model("anthropic/claude-opus-4.6")

    assert result["ok"] is True
    assert result["provider"] == "nous"
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"
    assert saved["model"]["default"] == "anthropic/claude-opus-4.6"


# ── Gap 2: POST /api/session/update ─────────────────────────────────────────

class _Session:
    """Minimal session double for the /api/session/update route."""

    def __init__(self, **kw):
        self.session_id = kw.pop("session_id", "s9314")
        self.model = kw.pop("model", "meituan/longcat-2.5:free")
        self.model_provider = kw.pop("model_provider", "nous")
        self.workspace = kw.pop("workspace", "/tmp")
        self.last_used_model = kw.pop("last_used_model", None)
        self.gateway_routing = kw.pop("gateway_routing", None)
        self.gateway_routing_history = kw.pop("gateway_routing_history", [])
        self.read_only = False
        self.profile = None
        self.messages = []
        self.__dict__.update(kw)

    def compact(self):
        return {"session_id": self.session_id, "model": self.model,
                "model_provider": self.model_provider, "messages": []}

    def save(self):
        pass


def _session_update(monkeypatch, session, body):
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(routes, "get_session", lambda sid, *a, **kw: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **kw: "/tmp")
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_publish_saved_session", lambda *a, **kw: None)
    handled = routes.handle_post(object(), type("P", (), {"path": "/api/session/update", "query": ""})())
    assert handled is True
    return captured


def test_session_update_drops_a_picker_model_pin(monkeypatch):
    session = _Session()
    captured = _session_update(
        monkeypatch,
        session,
        {
            "session_id": "s9314",
            "model": "meituan/longcat-2.5:free",
            "model_provider": "nous:meituan/longcat-2.5",
        },
    )
    assert captured["status"] == 200, captured
    assert session.model_provider == "nous", session.model_provider
    assert captured["payload"]["session"]["model_provider"] == "nous"


def test_session_update_keeps_a_declared_colon_provider_verbatim(monkeypatch, _isolate_config):
    _seed_config(
        _isolate_config,
        {
            "model": {"provider": "openrouter:eu", "default": "gpt-x"},
            "providers": {"openrouter:eu": {"base_url": "https://eu-proxy.example/v1"}},
        },
    )
    session = _Session(model="gpt-x", model_provider="openrouter")

    _session_update(
        monkeypatch,
        session,
        {"session_id": "s9314", "model": "gpt-x", "model_provider": "openrouter:eu"},
    )

    assert session.model_provider == "openrouter:eu", session.model_provider


# ── Gap 2: profile creation ─────────────────────────────────────────────────

def test_profile_default_model_writer_drops_a_picker_model_pin(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="meituan/longcat-2.5:free",
        model_provider="nous:meituan/longcat-2.5",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous", saved["model"]
    assert saved["model"]["default"] == "meituan/longcat-2.5:free"


def test_profile_default_model_writer_keeps_a_declared_colon_provider(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"providers": {"openrouter:eu": {"base_url": "https://eu-proxy.example/v1"}}}
        ),
        encoding="utf-8",
    )

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="gpt-x",
        model_provider="openrouter:eu",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "openrouter:eu", saved["model"]


def test_profile_creation_persists_the_provider_without_the_pin(tmp_path, monkeypatch):
    """End to end through create_profile_api: picker pin in, bare provider stored."""
    fake_home = tmp_path / "hermes-home"
    (fake_home / "profiles").mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", fake_home)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(profiles, "invalidate_profile_skills_stats", lambda *a, **kw: None)
    monkeypatch.setattr(profiles, "_invalidate_list_profiles_cache", lambda *a, **kw: None)
    monkeypatch.setattr(profiles, "_invalidate_root_profile_cache", lambda *a, **kw: None)
    monkeypatch.setattr(profiles, "_drop_profile_models_cache", lambda *a, **kw: None)
    monkeypatch.setattr(
        profiles,
        "_get_available_models_for_profile_validation",
        lambda: {
            "groups": [
                {
                    "provider": "Nous",
                    "provider_id": "nous",
                    "models": [{"id": "meituan/longcat-2.5:free", "label": "LongCat"}],
                }
            ]
        },
    )

    def fake_create(name, **kw):
        (fake_home / "profiles" / name).mkdir(parents=True, exist_ok=True)

    def fake_seed(profile_path, quiet=None):
        return None

    mock = ModuleType("hermes_cli.profiles")
    mock.create_profile = fake_create
    mock.seed_profile_skills = fake_seed
    saved_modules = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.profiles")}
    sys.modules["hermes_cli"] = ModuleType("hermes_cli")
    sys.modules["hermes_cli.profiles"] = mock
    try:
        profiles.create_profile_api(
            "research",
            default_model="meituan/longcat-2.5:free",
            model_provider="nous:meituan/longcat-2.5",
        )
    finally:
        for key, module in saved_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    saved = yaml.safe_load(
        (fake_home / "profiles" / "research" / "config.yaml").read_text(encoding="utf-8")
    )
    assert saved["model"]["provider"] == "nous", saved["model"]
    assert saved["model"]["default"] == "meituan/longcat-2.5:free"
