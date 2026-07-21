"""Public behavior for the profile-scoped composer Fast mode API."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import yaml


@pytest.fixture(autouse=True)
def _reset_config_caches():
    from api import config

    def reset():
        with config._cfg_lock:
            config._cfg_cache.clear()
            config.cfg = config._cfg_cache
            config._cfg_fingerprint = None
            config._cfg_mtime = 0.0
        with config._yaml_file_cache_lock:
            config._yaml_file_cache.clear()

    reset()
    yield
    reset()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fast_mode_status_is_gated_by_effective_selection(monkeypatch, tmp_path):
    from api import config

    path = tmp_path / "config.yaml"
    _write(path, "model:\n  provider: openai\n  default: gpt-5.5\n  service_tier: priority\n")
    monkeypatch.setattr(config, "_get_config_path", lambda: path)

    supported = config.get_fast_mode_status(model_id="gpt-5.5", provider_id="openai")
    unsupported = config.get_fast_mode_status(
        model_id="anthropic/claude-sonnet-4.6", provider_id="anthropic"
    )

    assert supported == {"supported": True, "enabled": True, "service_tier": "priority"}
    assert unsupported == {"supported": False, "enabled": False, "service_tier": "priority"}


def test_fast_mode_mutation_changes_only_service_tier(monkeypatch, tmp_path):
    from api import config

    path = tmp_path / "config.yaml"
    original = {
        "model": {
            "provider": "openai",
            "default": "gpt-5.5",
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "service_tier": "default",
        },
        "agent": {"max_tokens": 1234},
    }
    _write(path, yaml.safe_dump(original, sort_keys=False))
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "reload_config", lambda: None)

    enabled = config.set_fast_mode(True, model_id="gpt-5.5", provider_id="openai")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert enabled["enabled"] is True
    assert saved["model"] == {**original["model"], "service_tier": "priority"}
    assert saved["agent"] == original["agent"]

    disabled = config.set_fast_mode(
        False, model_id="anthropic/claude-sonnet-4.6", provider_id="anthropic"
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert disabled == {"supported": False, "enabled": False, "service_tier": ""}
    assert not saved["model"].get("service_tier")
    for key in ("provider", "default", "base_url", "api_key"):
        assert saved["model"][key] == original["model"][key]


def test_fast_mode_enable_falls_back_to_saved_default(monkeypatch, tmp_path):
    from api import config

    path = tmp_path / "config.yaml"
    _write(path, "model:\n  provider: openai\n  default: gpt-5.5\n")
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "reload_config", lambda: None)

    status = config.set_fast_mode(True)

    assert status == {"supported": True, "enabled": True, "service_tier": "priority"}
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["model"]["service_tier"] == "priority"


def test_fast_mode_rejects_unsupported_enable_without_writing(monkeypatch, tmp_path):
    from api import config

    path = tmp_path / "config.yaml"
    text = "model:\n  provider: openai\n  default: gpt-5.5\n"
    _write(path, text)
    monkeypatch.setattr(config, "_get_config_path", lambda: path)

    with pytest.raises(ValueError, match="does not support Fast mode"):
        config.set_fast_mode(True, model_id="gpt-5.3-codex", provider_id="openai-codex")
    assert path.read_text(encoding="utf-8") == text

    with pytest.raises(ValueError, match="does not support Fast mode"):
        config.set_fast_mode(True, model_id="gpt-5.5", provider_id="openrouter")
    assert path.read_text(encoding="utf-8") == text


def test_fast_mode_rejects_partial_or_mismatched_override_identity(monkeypatch, tmp_path):
    from api import config

    path = tmp_path / "config.yaml"
    text = "model:\n  provider: openai\n  default: gpt-5.5\n"
    _write(path, text)
    monkeypatch.setattr(config, "_get_config_path", lambda: path)

    matched = config.get_fast_mode_status(
        model_id="@openai:gpt-5.5", provider_id="openai"
    )
    assert matched["supported"] is True

    for kwargs in (
        {"model_id": "gpt-5.5"},
        {"provider_id": "openai"},
        {"model_id": "anthropic/claude-sonnet-4.6", "provider_id": "openai"},
        {"model_id": "@anthropic:gpt-5.5", "provider_id": "openai"},
        {"model_id": "gpt-5.5", "provider_id": "unknown-provider"},
    ):
        status = config.get_fast_mode_status(**kwargs)
        assert status["supported"] is False
        assert status["enabled"] is False
        with pytest.raises(ValueError, match="does not support Fast mode"):
            config.set_fast_mode(True, **kwargs)
        assert path.read_text(encoding="utf-8") == text


def test_request_time_fast_mode_gating_requires_a_complete_matching_pair():
    from api import config

    saved = {
        "model": {
            "provider": "openai",
            "default": "gpt-5.5",
            "service_tier": "priority",
        }
    }

    assert config._main_model_request_overrides(saved)["service_tier"] == "priority"
    assert config._main_model_request_overrides(
        saved, effective_model="gpt-5.6-sol", effective_provider="openai"
    )["service_tier"] == "priority"
    for model, provider in (
        ("gpt-5.6-sol", None),
        (None, "openai"),
        ("@anthropic:gpt-5.6-sol", "openai"),
        ("anthropic/gpt-5.6-sol", "openai"),
    ):
        assert "service_tier" not in config._main_model_request_overrides(
            saved, effective_model=model, effective_provider=provider
        )


def test_fast_mode_uses_canonical_writer_and_preserves_symlink_metadata(
    monkeypatch, tmp_path
):
    from api import config

    candidates = []
    configured_python = os.getenv("HERMES_WEBUI_TEST_PYTHON", "").strip()
    if configured_python:
        candidates.append(Path(configured_python).expanduser().resolve().parents[2])
    configured_agent = os.getenv("HERMES_WEBUI_AGENT_DIR", "").strip()
    if configured_agent:
        candidates.append(Path(configured_agent).expanduser())
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    agent_dir = next((path for path in candidates if (path / "utils.py").is_file()), None)
    if agent_dir is None:
        pytest.skip("Hermes Agent canonical YAML writer is unavailable")
    monkeypatch.syspath_prepend(str(agent_dir))
    sys.modules.pop("utils", None)

    target = tmp_path / "managed-config.yaml"
    target.write_text(
        "# keep this comment\nmodel:\n  provider: openai\n  default: gpt-5.5\ndisplay:\n  show_reasoning: false\n",
        encoding="utf-8",
    )
    target.chmod(0o640)
    path = tmp_path / "config.yaml"
    path.symlink_to(target)
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "reload_config", lambda: None)

    config.set_fast_mode(True)

    assert path.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o640
    saved = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert saved["display"] == {"show_reasoning": False}
    assert saved["model"]["service_tier"] == "priority"


def test_fast_mode_uses_active_profile_config_path(monkeypatch, tmp_path):
    from api import config

    default_path = tmp_path / "config.yaml"
    work_path = tmp_path / "profiles" / "work" / "config.yaml"
    _write(default_path, "model:\n  provider: openai\n  default: gpt-5.5\n")
    _write(work_path, "model:\n  provider: openai\n  default: gpt-5.5\n")
    active = {"path": default_path}
    monkeypatch.setattr(config, "_get_config_path", lambda: active["path"])
    monkeypatch.setattr(config, "reload_config", lambda: None)

    active["path"] = work_path
    config.set_fast_mode(True, model_id="gpt-5.5", provider_id="openai")

    assert "service_tier" not in default_path.read_text(encoding="utf-8")
    assert "service_tier: priority" in work_path.read_text(encoding="utf-8")


def test_fast_mode_get_route_forwards_effective_identity(monkeypatch):
    from api import routes

    captured = {}
    monkeypatch.setattr(
        routes,
        "get_fast_mode_status",
        lambda **kwargs: captured.setdefault("status_kwargs", kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: captured.setdefault("payload", payload),
    )

    result = routes.handle_get(
        SimpleNamespace(headers={}),
        urlparse("/api/fast-mode?model=gpt-5.6-sol&provider=openai"),
    )

    assert result == {"model_id": "gpt-5.6-sol", "provider_id": "openai"}
    assert captured["status_kwargs"] == result


def test_fast_mode_post_route_forwards_boolean_and_identity(monkeypatch):
    from api import routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {
            "enabled": True,
            "model": "gpt-5.6-sol",
            "provider": "openai",
        },
    )
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        routes,
        "set_fast_mode",
        lambda enabled, **kwargs: captured.setdefault(
            "mutation", {"enabled": enabled, **kwargs}
        ),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: captured.setdefault("payload", payload),
    )

    result = routes.handle_post(
        SimpleNamespace(headers={}), urlparse("/api/fast-mode")
    )

    assert result == {
        "enabled": True,
        "model_id": "gpt-5.6-sol",
        "provider_id": "openai",
    }
    assert captured["mutation"] == result
