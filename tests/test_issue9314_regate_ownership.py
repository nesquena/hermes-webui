"""Spec for the #9318 review (maintainer, 2026-11-23T18:24:00Z).

Four behaviour items the maintainer named on the head that carries
``8c143eaea``:

1. the error's suggested fix must work — re-saving the provider that is
   already stored has to repair it instead of answering 400 forever;
2. a ``model_provider: null`` on ``/api/session/update`` must keep the
   session's route instead of clearing it;
3. a provider the config declares under a colon **name** inside
   ``providers:`` must be kept verbatim, as the Agent routes it;
4. the session writer must normalise against the session's own profile
   config, not the global one.

Every case is written against the public helpers and the real handlers; the
controls pin the behaviour that must survive (a different undeclared pin is
still named, a real provider edit still applies).
"""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import api.config as config
import api.profiles as profiles
import api.routes as routes
from api.config import normalize_provider_for_persistence


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "model:\n  provider: nous\n  base_url: https://router.nous.ai/v1\n"
        "  default: anthropic/claude-opus-4.6\n",
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
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    config._yaml_file_cache.clear()
    config.cfg.clear()
    config.cfg.update(yaml.safe_load(config_file.read_text(encoding="utf-8")))
    config._cfg_mtime = config_file.stat().st_mtime
    return data


def _saved_model(config_file: Path) -> dict:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    return data.get("model") or {}


def _post(path: str, body: dict) -> tuple[dict, int]:
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


class _Session:
    def __init__(self, **kw):
        self.session_id = kw.pop("session_id", "s9318")
        self.model = kw.pop("model", "gpt-x")
        self.model_provider = kw.pop("model_provider", "nous")
        self.workspace = kw.pop("workspace", "/tmp")
        self.last_used_model = kw.pop("last_used_model", None)
        self.gateway_routing = kw.pop("gateway_routing", None)
        self.gateway_routing_history = kw.pop("gateway_routing_history", [])
        self.read_only = False
        self.profile = kw.pop("profile", None)
        self.messages = []
        self.__dict__.update(kw)

    def compact(self):
        return {
            "session_id": self.session_id,
            "model": self.model,
            "model_provider": self.model_provider,
            "messages": [],
        }

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
    handled = routes.handle_post(
        object(), type("P", (), {"path": "/api/session/update", "query": ""})()
    )
    assert handled is True
    return captured


# ── 1) the suggested fix works: re-saving the stored value repairs it ───────

def test_resaving_the_stored_broken_provider_repairs_it(_isolate_config):
    """Settings › Main model options › Save must repair, not answer 400 forever."""
    config_file = _isolate_config
    _seed_config(
        config_file,
        {"model": {"provider": "openrouter:eu", "default": "anthropic/claude-3.5-sonnet"}},
    )

    payload, status = _post(
        "/api/default-model",
        {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter:eu"},
    )

    assert status == 200, (status, payload)
    saved = _saved_model(config_file)
    assert saved["provider"] == "openrouter"
    assert saved["default"] == "anthropic/claude-3.5-sonnet"


def test_resaving_also_repairs_through_the_model_set_route(_isolate_config):
    config_file = _isolate_config
    _seed_config(
        config_file,
        {"model": {"provider": "openrouter:eu", "default": "anthropic/claude-3.5-sonnet"}},
    )

    payload, status = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openrouter:eu",
            "model": "anthropic/claude-3.5-sonnet",
        },
    )

    assert status == 200, (status, payload)
    assert _saved_model(config_file)["provider"] == "openrouter"


def test_a_different_undeclared_pin_is_still_refused_and_named(_isolate_config):
    """Control: the naming path must survive for a value that is not the saved one."""
    config_file = _isolate_config
    _seed_config(config_file, {"model": {"provider": "nous", "default": "old-model"}})

    payload, status = _post(
        "/api/default-model", {"model": "new-model", "provider": "openrouter:eu"}
    )

    assert status == 400, (status, payload)
    assert "openrouter:eu" in payload["error"], payload
    saved = _saved_model(config_file)
    assert saved["provider"] == "nous"
    assert saved["default"] == "old-model"


# ── 2) a null provider keeps the session's route ────────────────────────────

def test_session_update_with_a_null_provider_keeps_the_route(monkeypatch):
    session = _Session(model="meituan/longcat-2.5:free", model_provider="nous")
    evicted: list[str] = []
    monkeypatch.setattr(config, "_evict_session_agent", lambda sid: evicted.append(sid))

    captured = _session_update(
        monkeypatch,
        session,
        {
            "session_id": "s9318",
            "model": "meituan/longcat-2.5:free",
            "model_provider": None,
        },
    )

    assert captured["status"] == 200, captured
    assert session.model_provider == "nous", session.model_provider
    assert evicted == []
    assert captured["payload"]["session"]["model_provider"] == "nous"


def test_session_update_with_a_real_provider_still_applies_it(monkeypatch):
    """Control: an explicit provider in the body still reaches the session."""
    session = _Session(model="gpt-x", model_provider="nous")

    captured = _session_update(
        monkeypatch,
        session,
        {"session_id": "s9318", "model": "gpt-x", "model_provider": "openrouter"},
    )

    assert captured["status"] == 200, captured
    assert session.model_provider == "openrouter"


# ── 3) a provider declared by colon NAME inside providers: ──────────────────

NAMED_ENTRY = {
    "model": {"provider": "openrouter", "default": "gpt-x"},
    "providers": {
        "oreu": {"name": "openrouter:eu", "base_url": "https://eu.example/v1"}
    },
}


def test_provider_declared_by_name_is_kept_verbatim_by_the_helper(_isolate_config):
    _seed_config(_isolate_config, NAMED_ENTRY)

    assert normalize_provider_for_persistence("openrouter:eu", config.cfg) == "openrouter:eu"


def test_provider_declared_by_name_is_accepted_and_stored_verbatim(_isolate_config):
    config_file = _isolate_config
    _seed_config(config_file, NAMED_ENTRY)

    payload, status = _post(
        "/api/default-model", {"model": "gpt-x", "provider": "openrouter:eu"}
    )

    assert status == 200, (status, payload)
    assert payload["provider"] == "openrouter:eu", payload
    assert _saved_model(config_file)["provider"] == "openrouter:eu"


def test_session_writer_keeps_a_provider_declared_by_name(monkeypatch, _isolate_config):
    _seed_config(_isolate_config, NAMED_ENTRY)
    session = _Session(model="gpt-x", model_provider="openrouter")

    _session_update(
        monkeypatch,
        session,
        {"session_id": "s9318", "model": "gpt-x", "model_provider": "openrouter:eu"},
    )

    assert session.model_provider == "openrouter:eu", session.model_provider


# ── 4) the session writer reads the session's own profile config ────────────

def test_session_writer_reads_the_session_profile_config(
    monkeypatch, tmp_path, _isolate_config
):
    """A provider only that profile declares must not be split by this writer."""
    _seed_config(_isolate_config, {"model": {"provider": "nous", "default": "gpt-x"}})
    profile_home = tmp_path / "profiles" / "research"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"providers": {"openrouter:eu": {"base_url": "https://eu2.example/v1"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: Path(str(profile_home))
    )

    session = _Session(model="gpt-x", model_provider="openrouter", profile="research")
    _session_update(
        monkeypatch,
        session,
        {"session_id": "s9318", "model": "gpt-x", "model_provider": "openrouter:eu"},
    )

    assert session.model_provider == "openrouter:eu", session.model_provider
