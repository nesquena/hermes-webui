"""Codex control-plane regression tests.

Covers:
- WebUI exposes the full Codex fallback catalog instead of a stale partial list.
- /api/codex/capabilities and /api/codex/select are routed.
- Selecting Codex persists provider/model/base_url/reasoning into config.yaml.
- /codex slash command is wired to the Codex API endpoints.
"""

from __future__ import annotations

import json
import io
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

import yaml

import api.config as config
import api.routes as routes


REPO = Path(__file__).parent.parent


def _capture_json(monkeypatch):
    captured = {}

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["data"] = data
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    return captured


def test_codex_catalog_includes_full_webui_fallback():
    ids = [m["id"] for m in config.get_codex_model_catalog(include_live=False)]

    for model_id in (
        "gpt-5.5",
        "gpt-5.4-pro",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-5.1-codex",
        "gpt-5-codex",
        "codex-mini-latest",
    ):
        assert model_id in ids
    assert len(ids) >= 16


def test_set_codex_model_and_reasoning_writes_provider_model_base_url_and_effort(
    monkeypatch, tmp_path
):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n"
        "agent:\n"
        "  reasoning_effort: low\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(
        config,
        "get_codex_model_ids_for_webui",
        lambda include_live=True: ["gpt-5.3-codex-spark", "gpt-5.5"],
    )
    monkeypatch.setattr(
        config,
        "get_codex_capabilities",
        lambda include_live=False: {
            "ok": True,
            "current_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "xhigh",
        },
    )

    result = config.set_codex_model_and_reasoning("@openai-codex:gpt-5.3-codex-spark", "xhigh")

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["provider"] == "openai-codex"
    assert written["model"]["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert written["model"]["default"] == "gpt-5.3-codex-spark"
    assert written["agent"]["reasoning_effort"] == "xhigh"
    assert result["selected_model"] == "gpt-5.3-codex-spark"
    assert result["selected_effort"] == "xhigh"
    assert result["model_recognized"] is True


def test_codex_effort_only_switch_chooses_codex_default_when_previous_provider_is_not_codex(
    monkeypatch, tmp_path
):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-opus-4.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(
        config,
        "get_codex_model_ids_for_webui",
        lambda include_live=True: ["gpt-5.5", "gpt-5.3-codex"],
    )
    monkeypatch.setattr(
        config,
        "get_codex_capabilities",
        lambda include_live=False: {
            "ok": True,
            "current_model": "gpt-5.5",
            "reasoning_effort": "high",
        },
    )

    config.set_codex_model_and_reasoning(effort="high")

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["provider"] == "openai-codex"
    assert written["model"]["default"] == "gpt-5.5"
    assert written["agent"]["reasoning_effort"] == "high"


def test_codex_capabilities_get_endpoint_is_registered(monkeypatch):
    captured = _capture_json(monkeypatch)
    monkeypatch.setattr(
        routes,
        "get_codex_capabilities",
        lambda include_live=True: {"ok": True, "live": include_live},
    )

    handled = routes.handle_get(mock.MagicMock(), urlparse("/api/codex/capabilities?live=0"))

    assert handled is True
    assert captured["data"] == {"ok": True, "live": False}


def test_codex_select_post_endpoint_is_registered(monkeypatch):
    captured = _capture_json(monkeypatch)
    handler = mock.MagicMock()
    body = json.dumps(
        {"model": "gpt-5.3-codex", "effort": "medium"}
    ).encode("utf-8")
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    monkeypatch.setattr(
        routes,
        "set_codex_model_and_reasoning",
        lambda model, effort: {"ok": True, "model": model, "effort": effort},
    )

    handled = routes.handle_post(handler, urlparse("/api/codex/select"))

    assert handled is True
    assert captured["data"] == {"ok": True, "model": "gpt-5.3-codex", "effort": "medium"}


def test_live_models_openai_codex_uses_control_plane_catalog(monkeypatch):
    captured = _capture_json(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_codex_model_catalog",
        lambda include_live=True: [{"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"}],
    )
    parsed = mock.MagicMock()
    parsed.query = "provider=openai-codex"

    handled = routes._handle_live_models(mock.MagicMock(), parsed)

    assert handled is True
    assert captured["data"]["provider"] == "openai-codex"
    assert captured["data"]["models"] == [
        {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"}
    ]


def test_codex_slash_command_is_wired_to_capabilities_and_select_api():
    src = (REPO / "static" / "commands.js").read_text(encoding="utf-8")

    assert "{name:'codex'" in src
    assert "fn:cmdCodex" in src
    assert "subArgs:'codexModels'" in src
    assert "function cmdCodex" in src
    assert "/api/codex/capabilities" in src
    assert "/api/codex/select" in src
    assert "method:'POST'" in src


def test_codex_command_i18n_key_exists():
    i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "cmd_codex" in i18n


def test_provider_mismatch_treats_openai_codex_as_openai_family():
    ui = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert "'openai-codex':'openai'" in ui
    assert "'codex':'openai'" in ui
