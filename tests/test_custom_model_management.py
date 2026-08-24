"""Custom model/provider CRUD and test contracts for Settings → Providers."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml


@contextmanager
def _no_lock(_profile_key):
    yield


def _install_isolated_home(monkeypatch, tmp_path):
    from api import custom_models

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(custom_models, "_active_home", lambda: home)
    monkeypatch.setattr(custom_models, "_shared_profile_mutation_lock", _no_lock)
    monkeypatch.setattr(custom_models, "_refresh_runtime_caches", lambda *_a, **_k: None)
    return custom_models, home


def test_create_custom_model_keeps_secret_out_of_yaml(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)

    result = custom_models.upsert_custom_model({
        "name": "Example Router",
        "base_url": "https://router.example/v1/",
        "api_mode": "chat_completions",
        "models": ["model-a", "model-b"],
        "default_model": "model-b",
        "api_key": "secret-value-123456",
        "make_default": True,
    })

    assert result["ok"] is True
    assert result["provider"]["provider_id"] == "custom:example-router"
    assert result["provider"]["default_model"] == "model-b"
    assert result["provider"]["has_api_key"] is True
    assert "api_key" not in result["provider"]

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    entry = raw["providers"]["custom:example-router"]
    assert entry["api"] == "https://router.example/v1"
    assert entry["transport"] == "chat_completions"
    assert entry["default_model"] == "model-b"
    assert "enabled" not in entry
    assert entry["key_env"].startswith("HERMES_CUSTOM_EXAMPLE_ROUTER_")
    assert entry["key_env"].endswith("_API_KEY")
    assert "api_key" not in entry
    assert raw["model"]["provider"] == "custom:example-router"
    assert raw["model"]["default"] == "model-b"
    assert "secret-value-123456" not in (home / "config.yaml").read_text(encoding="utf-8")
    assert f'{entry["key_env"]}=secret-value-123456' in (
        home / ".env"
    ).read_text(encoding="utf-8")


def test_list_redacts_legacy_inline_and_env_credentials(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "custom_providers": [
                {
                    "name": "Legacy One",
                    "base_url": "https://legacy.example/v1",
                    "model": "legacy-model",
                    "api_key": "legacy-inline-secret",
                },
                {
                    "name": "Legacy Two",
                    "base_url": "https://legacy-two.example/v1",
                    "models": {"m1": {}, "m2": {"context_length": 32000}},
                    "key_env": "LEGACY_TWO_KEY",
                },
            ]
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("LEGACY_TWO_KEY=another-secret\n", encoding="utf-8")

    payload = custom_models.list_custom_models()
    encoded = json.dumps(payload)
    assert "legacy-inline-secret" not in encoded
    assert "another-secret" not in encoded
    assert [row["provider_id"] for row in payload["providers"]] == [
        "custom:legacy-one",
        "custom:legacy-two",
    ]
    assert all(row["has_api_key"] for row in payload["providers"])
    assert payload["providers"][1]["models"] == ["m1", "m2"]


def test_edit_preserves_unknown_fields_and_stored_key(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "custom:router": {
                    "name": "Router",
                    "api": "https://old.example/v1",
                    "transport": "chat_completions",
                    "default_model": "old-model",
                    "models": {"old-model": {"context_length": 8000}},
                    "key_env": "ROUTER_KEY",
                    "extra_headers": {"X-Tenant": "power"},
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("ROUTER_KEY=stored-secret\n", encoding="utf-8")

    result = custom_models.upsert_custom_model({
        "uid": "providers:custom:router",
        "name": "Router Updated",
        "base_url": "https://old.example/v2",
        "api_mode": "chat_completions",
        "models": ["new-model", "old-model"],
        "default_model": "new-model",
    })

    assert result["provider"]["has_api_key"] is True
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    entry = raw["providers"]["custom:router"]
    assert entry["extra_headers"] == {"X-Tenant": "power"}
    assert entry["key_env"] == "ROUTER_KEY"
    assert entry["models"]["old-model"]["context_length"] == 8000
    assert entry["models"]["new-model"] == {}


def test_legacy_generic_custom_active_provider_matches_by_base_url(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "model": {
                "provider": "custom",
                "default": "active-model",
                "base_url": "https://active.example/v1/",
            },
            "custom_providers": [
                {
                    "name": "Active endpoint",
                    "base_url": "https://active.example/v1",
                    "model": "active-model",
                },
                {
                    "name": "Other endpoint",
                    "base_url": "https://other.example/v1",
                    "model": "other-model",
                },
            ],
        }, sort_keys=False),
        encoding="utf-8",
    )

    listed = custom_models.list_custom_models()
    active = [provider for provider in listed["providers"] if provider["is_active"]]
    assert [provider["provider_id"] for provider in active] == ["custom:active-endpoint"]

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.delete_custom_model(
            {"uid": "custom_providers:custom:active-endpoint"}
        )
    assert excinfo.value.status == 409


def test_modern_storage_key_alias_is_active_and_delete_protected(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "model": {"provider": "custom:router", "default": "m1"},
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://router.example/v1",
                    "transport": "chat_completions",
                    "default_model": "m1",
                    "models": {"m1": {}},
                }
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    listed = custom_models.list_custom_models()
    assert listed["providers"][0]["provider_id"] == "custom:router"
    assert listed["providers"][0]["is_active"] is True
    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.delete_custom_model({"uid": "providers:router"})
    assert excinfo.value.status == 409


def test_delete_active_provider_fails_closed(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "model": {"provider": "custom:router", "default": "m1"},
            "providers": {
                "custom:router": {
                    "name": "Router",
                    "base_url": "https://router.example/v1",
                    "model": "m1",
                }
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    try:
        custom_models.delete_custom_model({"uid": "providers:custom:router"})
    except custom_models.CustomModelError as exc:
        assert exc.status == 409
        assert "default" in str(exc).lower()
    else:
        raise AssertionError("active custom provider deletion must be rejected")

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "custom:router" in raw["providers"]


def test_discovery_uses_draft_url_and_key_and_returns_selectable_models(
    monkeypatch, tmp_path
):
    custom_models, _home = _install_isolated_home(monkeypatch, tmp_path)
    captured = {}

    def fake_probe(provider, base_url, api_key, timeout):
        captured.update(
            {
                "provider": provider,
                "base_url": base_url,
                "api_key": api_key,
                "timeout": timeout,
            }
        )
        return {
            "ok": True,
            "models": [
                {"id": "model-a", "label": "Model A"},
                {"id": "model-b", "label": "Model B"},
            ],
        }

    monkeypatch.setattr("api.provider_endpoint_probe.probe_models_endpoint", fake_probe)
    result = custom_models.discover_custom_models(
        {
            "name": "Draft",
            "base_url": "https://draft.example/v1",
            "api_mode": "chat_completions",
            "api_key": "draft-secret-value",
        }
    )

    assert result == {"ok": True, "models": ["model-a", "model-b"], "count": 2}
    assert captured == {
        "provider": "custom",
        "base_url": "https://draft.example/v1",
        "api_key": "draft-secret-value",
        "timeout": 8.0,
    }
    assert "draft-secret-value" not in json.dumps(result)


def test_discovery_uses_anthropic_auth_mode(monkeypatch, tmp_path):
    custom_models, _home = _install_isolated_home(monkeypatch, tmp_path)
    captured = {}

    def fake_probe(provider, base_url, api_key, timeout):
        captured["provider"] = provider
        return {"ok": True, "models": [{"id": "claude-model"}]}

    monkeypatch.setattr("api.provider_endpoint_probe.probe_models_endpoint", fake_probe)
    result = custom_models.discover_custom_models(
        {
            "base_url": "https://api.anthropic.com/v1",
            "api_mode": "anthropic_messages",
            "api_key": "anthropic-secret",
        }
    )
    assert result["models"] == ["claude-model"]
    assert captured["provider"] == "anthropic"


def test_inference_http_error_never_reflects_upstream_body(monkeypatch):
    import io
    import urllib.error
    from email.message import Message

    from api import custom_models

    class FailingOpener:
        def open(self, request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=Message(),
                fp=io.BytesIO(b"upstream-secret-body-value"),
            )

    monkeypatch.setattr(custom_models, "_JSON_OPENER", FailingOpener())
    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models._json_request(
            "https://router.example/v1/chat/completions",
            method="POST",
            headers={"Content-Type": "application/json"},
            payload={"model": "m1"},
            timeout=1,
        )
    assert str(excinfo.value) == "Endpoint returned HTTP 401."
    assert "upstream-secret-body-value" not in str(excinfo.value)


def test_inference_test_uses_draft_key_without_returning_it(monkeypatch, tmp_path):
    custom_models, _home = _install_isolated_home(monkeypatch, tmp_path)
    captured = {}

    def fake_request(url, *, method, headers, payload, timeout):
        captured.update({
            "url": url,
            "method": method,
            "headers": headers,
            "payload": payload,
            "timeout": timeout,
        })
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(custom_models, "_json_request", fake_request)
    result = custom_models.test_custom_model({
        "name": "Draft",
        "base_url": "https://draft.example/v1",
        "api_mode": "chat_completions",
        "model": "draft-model",
        "api_key": "draft-secret-value",
    })

    assert result["ok"] is True
    assert result["inference_ok"] is True
    assert captured["url"] == "https://draft.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer draft-secret-value"
    assert captured["headers"]["User-Agent"] == "Hermes-WebUI/1.0"
    assert captured["payload"]["model"] == "draft-model"
    assert "draft-secret-value" not in json.dumps(result)


def test_unknown_or_auto_protocol_is_preserved_without_forcing_chat(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router", "api": "https://router.example/v1",
                    "transport": "auto", "default_model": "m1",
                    "models": {"m1": {}},
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    listed = custom_models.list_custom_models()
    assert listed["providers"][0]["api_mode"] == "auto"
    assert listed["providers"][0]["test_supported"] is False
    custom_models.upsert_custom_model({
        "uid": "providers:router", "name": "Router",
        "base_url": "https://router.example/v2", "api_mode": "auto",
        "models": ["m1"], "default_model": "m1",
    })
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert raw["providers"]["router"]["transport"] == "auto"


def test_modern_unknown_transport_and_list_model_metadata_survive_edit(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    original_model = {
        "id": "m1",
        "label": "Friendly Model",
        "capabilities": ["tools"],
        "context_length": 64000,
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://router.example/v1",
                    "transport": "bedrock_converse",
                    "default_model": "m1",
                    "models": [original_model],
                    "discover_models": False,
                    "extra_body": {"tenant": "alpha"},
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )

    listed = custom_models.list_custom_models()
    assert listed["providers"][0]["context_length"] == 64000

    custom_models.upsert_custom_model({
        "uid": "providers:router",
        "name": "Router",
        "base_url": "https://router.example/v1",
        "api_mode": "bedrock_converse",
        "models": ["m1"],
        "default_model": "m1",
        "context_length": 64000,
    })

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    entry = raw["providers"]["router"]
    assert entry["transport"] == "bedrock_converse"
    assert entry["discover_models"] is False
    assert entry["models"] == [original_model]
    assert entry["extra_body"] == {"tenant": "alpha"}
    assert "base_url" not in entry
    assert "api_mode" not in entry
    assert "model" not in entry


def test_omitted_modern_transport_remains_omitted_when_unchanged(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://router.example/v1",
                    "default_model": "m1",
                    "models": {"m1": {}},
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )

    listed = custom_models.list_custom_models()["providers"][0]
    assert listed["api_mode"] == "auto"
    assert listed["api_mode_explicit"] is False
    custom_models.upsert_custom_model({
        "uid": "providers:router",
        "name": "Router",
        "base_url": "https://router.example/v1",
        "models": ["m1"],
        "default_model": "m1",
    })
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "transport" not in raw["providers"]["router"]


def test_modern_default_matches_paired_agent_runtime_resolver(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    custom_models.upsert_custom_model({
        "name": "Runtime Router",
        "base_url": "https://runtime.example/v1",
        "api_mode": "anthropic_messages",
        "models": ["m1", "m2"],
        "default_model": "m2",
    })
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))

    from hermes_cli import runtime_provider

    monkeypatch.setattr(runtime_provider, "load_config", lambda: raw)
    resolved = runtime_provider._get_named_custom_provider(
        "custom:runtime-router"
    )
    assert resolved is not None
    assert resolved["base_url"] == "https://runtime.example/v1"
    assert resolved["model"] == "m2"
    assert resolved["api_mode"] == "anthropic_messages"


def test_disabled_custom_provider_cannot_be_activated(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "custom:disabled": {
                    "name": "Disabled",
                    "base_url": "https://disabled.example/v1",
                    "default_model": "m1",
                    "models": {"m1": {}},
                    "enabled": False,
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.activate_custom_model(
            {"uid": "providers:custom:disabled", "model": "m1"}
        )
    assert excinfo.value.status == 409


def test_renaming_legacy_provider_migrates_to_stable_modern_identity(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "custom_providers": [{
                "name": "Old Name",
                "base_url": "https://legacy.example/v1",
                "model": "m1",
                "extra_headers": {"X-Team": "alpha"},
            }]
        }, sort_keys=False),
        encoding="utf-8",
    )

    result = custom_models.upsert_custom_model({
        "uid": "custom_providers:custom:old-name",
        "name": "New Display Name",
        "base_url": "https://legacy.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
    })

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert raw.get("custom_providers") == []
    assert raw["providers"]["custom:old-name"]["name"] == "New Display Name"
    assert raw["providers"]["custom:old-name"]["extra_headers"] == {"X-Team": "alpha"}
    assert result["provider"]["uid"] == "providers:custom:old-name"


def test_clearing_shared_key_keeps_env_for_other_provider(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "custom:one": {
                    "name": "One", "base_url": "https://one.example/v1",
                    "model": "m1", "key_env": "SHARED_CUSTOM_KEY",
                },
                "custom:two": {
                    "name": "Two", "base_url": "https://two.example/v1",
                    "model": "m2", "key_env": "SHARED_CUSTOM_KEY",
                },
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("SHARED_CUSTOM_KEY=shared-secret\n", encoding="utf-8")

    custom_models.upsert_custom_model({
        "uid": "providers:custom:one",
        "name": "One",
        "base_url": "https://one.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
        "clear_api_key": True,
    })

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "key_env" not in raw["providers"]["custom:one"]
    assert raw["providers"]["custom:two"]["key_env"] == "SHARED_CUSTOM_KEY"
    assert "SHARED_CUSTOM_KEY=shared-secret" in (home / ".env").read_text(encoding="utf-8")


def test_delete_env_failure_leaves_provider_config_untouched(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "custom:one": {
                    "name": "One", "base_url": "https://one.example/v1",
                    "model": "m1", "key_env": "ONE_KEY",
                    "webui_managed_key_env": True,
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("ONE_KEY=one-secret\n", encoding="utf-8")
    monkeypatch.setattr(
        custom_models,
        "_write_env_file",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("env write failed")),
    )

    with pytest.raises(OSError, match="env write failed"):
        custom_models.delete_custom_model({"uid": "providers:custom:one"})
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "custom:one" in raw["providers"]


def test_mutation_preserves_unrelated_env_references_in_raw_yaml(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("UNRELATED_SECRET_REF", "must-not-be-materialized")
    (home / "config.yaml").write_text(
        'unrelated_token: "${UNRELATED_SECRET_REF}"\n', encoding="utf-8"
    )

    custom_models.upsert_custom_model({
        "name": "Raw Safe",
        "base_url": "https://raw.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
    })

    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "${UNRELATED_SECRET_REF}" in text
    assert "must-not-be-materialized" not in text


def test_malformed_config_fails_without_overwrite(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    original = "model: [unterminated\n"
    (home / "config.yaml").write_text(original, encoding="utf-8")

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "name": "Must Not Save",
            "base_url": "https://invalid.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
        })
    assert excinfo.value.status == 409
    assert (home / "config.yaml").read_text(encoding="utf-8") == original


def test_duplicate_yaml_keys_fail_closed_without_overwrite(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    original = (
        "model:\n  provider: openai\n"
        "model:\n  provider: anthropic\n"
        "unrelated: keep-me\n"
    )
    config_path = home / "config.yaml"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "name": "Duplicate Guard",
            "base_url": "https://duplicate.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
        })
    assert excinfo.value.status == 409
    assert config_path.read_text(encoding="utf-8") == original
    assert not (home / ".env").exists()


def test_invalid_provider_container_fails_without_overwrite(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    original = "providers: should-be-a-mapping\nunrelated: keep-me\n"
    (home / "config.yaml").write_text(original, encoding="utf-8")
    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "name": "Must Not Save",
            "base_url": "https://invalid.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
        })
    assert excinfo.value.status == 409
    assert (home / "config.yaml").read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "advanced_field,advanced_value",
    [
        ("extra_headers", {"Authorization": "Bearer stored-header-secret"}),
        ("extra_body", {"access_token": "stored-body-secret"}),
    ],
)
def test_draft_origin_change_never_forwards_stored_advanced_request_metadata(
    monkeypatch, tmp_path, advanced_field, advanced_value
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    entry = {
        "name": "Advanced",
        "api": "https://trusted.example/v1",
        "transport": "chat_completions",
        "default_model": "m1",
        "models": {"m1": {}},
        advanced_field: advanced_value,
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"advanced": entry}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        custom_models,
        "_json_request",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("network request must not be attempted")
        ),
    )

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.test_custom_model({
            "uid": "providers:advanced",
            "base_url": "https://attacker.example/v1",
            "api_mode": "chat_completions",
            "model": "m1",
            "api_key": "new-explicit-secret",
        })
    assert excinfo.value.status == 409
    assert "advanced request metadata" in str(excinfo.value)


@pytest.mark.parametrize(
    "advanced_field,advanced_value",
    [
        ("extra_headers", {"Authorization": "Bearer stored-header-secret"}),
        ("extra_body", {"access_token": "stored-body-secret"}),
    ],
)
def test_save_rejects_origin_change_with_stored_advanced_request_metadata(
    monkeypatch, tmp_path, advanced_field, advanced_value
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    original = {
        "providers": {
            "advanced": {
                "name": "Advanced",
                "api": "https://trusted.example/v1",
                "transport": "chat_completions",
                "default_model": "m1",
                "models": {"m1": {}},
                advanced_field: advanced_value,
            }
        }
    }
    config_path = home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(original, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "uid": "providers:advanced",
            "name": "Advanced",
            "base_url": "https://attacker.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
            "api_key": "new-explicit-secret",
        })
    assert excinfo.value.status == 409
    assert "advanced request metadata" in str(excinfo.value)
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


def test_changed_origin_requires_reentered_or_removed_key(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://trusted.example/v1",
                    "transport": "chat_completions",
                    "default_model": "m1",
                    "key_env": "ROUTER_KEY",
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("ROUTER_KEY=stored-secret\n", encoding="utf-8")
    body = {
        "uid": "providers:router",
        "name": "Router",
        "base_url": "https://attacker.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
    }

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model(body)
    assert excinfo.value.status == 409
    with pytest.raises(custom_models.CustomModelError):
        custom_models.test_custom_model({
            "uid": "providers:router",
            "base_url": "https://attacker.example/v1",
            "model": "m1",
        })
    monkeypatch.setattr(
        custom_models,
        "_json_request",
        lambda *_a, **_k: (200, {"choices": [{"message": {"content": "OK"}}]}),
    )
    cleared = custom_models.test_custom_model({
        "uid": "providers:router",
        "base_url": "https://attacker.example/v1",
        "model": "m1",
        "clear_api_key": True,
    })
    assert cleared["ok"] is True


def test_replacement_and_removal_key_flags_are_mutually_exclusive(monkeypatch, tmp_path):
    custom_models, _home = _install_isolated_home(monkeypatch, tmp_path)
    with pytest.raises(custom_models.CustomModelError, match="either"):
        custom_models.upsert_custom_model({
            "name": "Conflict",
            "base_url": "https://conflict.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
            "api_key": "replacement-secret",
            "clear_api_key": True,
        })


@pytest.mark.parametrize("action", ["discover", "test"])
def test_readonly_actions_reject_replacement_and_removal_together(
    monkeypatch, tmp_path, action
):
    custom_models, _home = _install_isolated_home(monkeypatch, tmp_path)
    body = {
        "base_url": "https://conflict.example/v1",
        "api_mode": "chat_completions",
        "api_key": "replacement-secret",
        "clear_api_key": True,
        "model": "m1",
    }
    target = (
        custom_models.discover_custom_models
        if action == "discover"
        else custom_models.test_custom_model
    )
    with pytest.raises(custom_models.CustomModelError, match="either"):
        target(body)


def test_custom_key_lookup_respects_profile_process_fallback_block(
    monkeypatch, tmp_path
):
    from api import config as webui_config

    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "isolated": {
                    "name": "Isolated",
                    "api": "https://isolated.example/v1",
                    "transport": "chat_completions",
                    "default_model": "m1",
                    "models": {"m1": {}},
                    "key_env": "SHARED_PROCESS_ONLY_KEY",
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHARED_PROCESS_ONLY_KEY", "process-default-secret")
    previous_env = getattr(webui_config._thread_ctx, "env", {}).copy()
    previous_block = bool(
        getattr(webui_config._thread_ctx, "block_process_env_fallback", False)
    )
    try:
        webui_config._set_thread_env(HERMES_HOME=str(home))
        webui_config._thread_ctx.block_process_env_fallback = True
        listed = custom_models.list_custom_models()
    finally:
        webui_config._thread_ctx.block_process_env_fallback = previous_block
        if previous_env:
            webui_config._set_thread_env(**previous_env)
        else:
            webui_config._clear_thread_env()

    assert listed["providers"][0]["has_api_key"] is False


def test_file_credential_write_does_not_mutate_process_environment(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    result = custom_models.upsert_custom_model({
        "name": "Profile Local",
        "base_url": "https://local.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
        "api_key": "profile-local-secret",
    })
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    key_env = raw["providers"]["custom:profile-local"]["key_env"]
    assert key_env not in __import__("os").environ
    assert result["provider"]["has_api_key"] is True


def test_generated_env_names_do_not_collapse_punctuation(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    for name in ("a-b", "a_b", "a.b"):
        custom_models.upsert_custom_model({
            "name": name,
            "base_url": f"https://{name.replace('_', '-')}.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
            "api_key": f"secret-for-{name}",
        })
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    key_envs = {entry["key_env"] for entry in raw["providers"].values()}
    assert len(key_envs) == 3


def test_replacing_imported_key_allocates_owned_env_without_overwriting_shared(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://router.example/v1",
                    "transport": "chat_completions",
                    "default_model": "m1",
                    "models": {"m1": {}},
                    "key_env": "OPENAI_API_KEY",
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "OPENAI_API_KEY=builtin-secret\n", encoding="utf-8"
    )

    custom_models.upsert_custom_model({
        "uid": "providers:router",
        "name": "Router",
        "base_url": "https://router.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1"],
        "default_model": "m1",
        "api_key": "replacement-secret",
    })

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    entry = raw["providers"]["router"]
    assert entry["key_env"] != "OPENAI_API_KEY"
    assert entry["webui_managed_key_env"] is True
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=builtin-secret" in env_text
    assert f'{entry["key_env"]}=replacement-secret' in env_text


def test_delete_never_removes_imported_builtin_env_key(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router", "api": "https://router.example/v1",
                    "transport": "chat_completions", "default_model": "m1",
                    "key_env": "OPENAI_API_KEY",
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text("OPENAI_API_KEY=builtin-secret\n", encoding="utf-8")
    custom_models.delete_custom_model({"uid": "providers:router"})
    assert "OPENAI_API_KEY=builtin-secret" in (home / ".env").read_text(encoding="utf-8")


def test_delete_keeps_owned_key_when_noncustom_provider_reuses_it(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    managed_key = "HERMES_CUSTOM_ROUTER_ABCDEF1234_API_KEY"
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "router": {
                    "name": "Router",
                    "api": "https://router.example/v1",
                    "transport": "chat_completions",
                    "default_model": "m1",
                    "models": {"m1": {}},
                    "key_env": managed_key,
                    "webui_managed_key_env": True,
                },
                "openai": {"key_env": managed_key},
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        f"{managed_key}=shared-secret\n", encoding="utf-8"
    )

    custom_models.delete_custom_model({"uid": "providers:router"})

    assert f"{managed_key}=shared-secret" in (
        home / ".env"
    ).read_text(encoding="utf-8")


def test_duplicate_legacy_names_have_stable_distinct_uids(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "custom_providers": [
                {"name": "Team", "base_url": "https://one.example/v1", "model": "m1"},
                {"name": "Team", "base_url": "https://two.example/v1", "model": "m2"},
            ]
        }, sort_keys=False),
        encoding="utf-8",
    )
    listed = custom_models.list_custom_models()
    uids = [provider["uid"] for provider in listed["providers"]]
    assert len(set(uids)) == 2
    custom_models.delete_custom_model({"uid": uids[0]})
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert len(raw["custom_providers"]) == 1


def test_yaml_post_publication_failure_keeps_matching_credential(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    real_save = custom_models._save_config

    def publish_then_fail(*args, **kwargs):
        real_save(*args, **kwargs)
        raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(custom_models, "_save_config", publish_then_fail)
    with pytest.raises(OSError, match="after replace"):
        custom_models.upsert_custom_model({
            "name": "Published",
            "base_url": "https://published.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
            "api_key": "published-secret",
        })

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    entry = raw["providers"]["custom:published"]
    assert f'{entry["key_env"]}=published-secret' in (
        home / ".env"
    ).read_text(encoding="utf-8")


def test_runtime_refresh_invalidates_all_provider_caches(monkeypatch):
    from api import custom_models, providers, routes

    calls = []
    monkeypatch.setattr(custom_models.webui_config, "reload_config", lambda: calls.append("reload"))
    monkeypatch.setattr(
        custom_models.webui_config,
        "invalidate_models_cache",
        lambda: calls.append("models"),
    )
    monkeypatch.setattr(
        custom_models.webui_config,
        "invalidate_provider_models_cache",
        lambda provider: calls.append(("provider-models", provider)),
    )
    monkeypatch.setattr(
        custom_models.webui_config,
        "invalidate_credential_pool_cache",
        lambda provider: calls.append(("credentials", provider)),
    )
    monkeypatch.setattr(
        providers,
        "invalidate_account_usage_status_cache",
        lambda provider: calls.append(("account", provider)),
    )
    monkeypatch.setattr(
        providers,
        "invalidate_providers_cache",
        lambda: calls.append("providers"),
    )
    monkeypatch.setattr(routes, "_clear_live_models_cache", lambda: calls.append("live"))

    custom_models._refresh_runtime_caches("custom:router")

    assert calls == [
        "reload",
        "models",
        ("provider-models", "custom:router"),
        ("credentials", "custom:router"),
        ("account", "custom:router"),
        "providers",
        "live",
    ]


def test_mutation_response_stays_bound_to_committed_profile(monkeypatch, tmp_path):
    from api import custom_models

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    (home_b / "config.yaml").write_text(
        yaml.safe_dump({
            "providers": {
                "other": {
                    "name": "Other",
                    "api": "https://other.example/v1",
                    "default_model": "other-model",
                    "models": {"other-model": {}},
                }
            }
        }, sort_keys=False),
        encoding="utf-8",
    )
    active_home = [home_a]
    monkeypatch.setattr(custom_models, "_active_home", lambda: active_home[0])
    monkeypatch.setattr(custom_models, "_shared_profile_mutation_lock", _no_lock)
    monkeypatch.setattr(
        custom_models,
        "_refresh_runtime_caches",
        lambda *_a, **_k: active_home.__setitem__(0, home_b),
    )

    result = custom_models.upsert_custom_model({
        "name": "Committed A",
        "base_url": "https://a.example/v1",
        "api_mode": "chat_completions",
        "models": ["a-model"],
        "default_model": "a-model",
    })

    assert result["provider"]["name"] == "Committed A"
    assert [row["name"] for row in result["providers"]] == ["Committed A"]
    assert active_home[0] == home_b


def test_exact_config_snapshot_detects_same_stat_external_edit(
    monkeypatch, tmp_path
):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    config_path = home / "config.yaml"
    original = "unrelated: old\n"
    external = "unrelated: new\n"
    assert len(original) == len(external)
    config_path.write_text(original, encoding="utf-8")
    initial_stat = config_path.stat()
    real_load_config = custom_models._load_config

    def load_then_publish_same_stat(profile_home):
        loaded = real_load_config(profile_home)
        config_path.write_text(external, encoding="utf-8")
        os.utime(
            config_path,
            ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns),
        )
        return loaded

    monkeypatch.setattr(custom_models, "_load_config", load_then_publish_same_stat)
    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "name": "Conflict Guard",
            "base_url": "https://conflict.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
            "api_key": "must-not-persist",
        })
    assert excinfo.value.status == 409
    assert config_path.read_text(encoding="utf-8") == external
    assert not (home / ".env").exists()


def test_custom_lock_delegates_to_canonical_profile_transaction(
    monkeypatch, tmp_path
):
    from api import custom_models, provider_transactions

    home = (tmp_path / "profile").resolve()
    home.mkdir()
    state = {"entered": False}
    monkeypatch.setattr(custom_models, "_active_home", lambda: home)

    @contextmanager
    def canonical_transaction(resolver):
        assert resolver() == home
        state["entered"] = True
        yield home

    monkeypatch.setattr(
        provider_transactions,
        "active_profile_transaction",
        canonical_transaction,
    )
    with custom_models._shared_profile_mutation_lock(home):
        assert state["entered"] is True


def test_profile_home_is_rechecked_after_lock_acquisition(monkeypatch, tmp_path):
    from api import custom_models

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    homes = iter((home_a, home_b))
    monkeypatch.setattr(custom_models, "_active_home", lambda: next(homes))
    monkeypatch.setattr(custom_models, "_shared_profile_mutation_lock", _no_lock)

    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "name": "Race",
            "base_url": "https://race.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
        })
    assert excinfo.value.status == 409
    assert not (home_a / "config.yaml").exists()
    assert not (home_b / "config.yaml").exists()


def test_stale_profile_action_is_rejected_before_mutation(monkeypatch, tmp_path):
    custom_models, home = _install_isolated_home(monkeypatch, tmp_path)
    from api import profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-b")
    with pytest.raises(custom_models.CustomModelError) as excinfo:
        custom_models.upsert_custom_model({
            "profile": "profile-a",
            "name": "Stale",
            "base_url": "https://stale.example/v1",
            "api_mode": "chat_completions",
            "models": ["m1"],
            "default_model": "m1",
        })
    assert excinfo.value.status == 409
    assert not (home / "config.yaml").exists()


def test_routes_expose_custom_model_management_contract():
    source = Path("api/routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/providers/custom-models"' in source
    assert '"/api/providers/custom-models/test"' in source
    assert '"/api/providers/custom-models/discover"' in source
    assert 'parsed.path == "/api/providers/custom-models/activate"' in source
    assert 'parsed.path == "/api/providers/custom-models/delete"' in source
