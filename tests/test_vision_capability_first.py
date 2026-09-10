"""Behavioral tests for the vision-capability-first settings toggle.

``agent.vision_capability_first`` (capability-first image routing, the WebUI
Settings checkbox next to Auxiliary Models) is persisted to the active
profile's ``config.yaml`` — the same key the agent router
(``agent/image_routing.py``) reads, not WebUI settings.json — so every surface
(CLI, gateway, WebUI) shares one value.

Pins:
  * set_vision_capability_first writes the key and preserves unrelated agent keys,
  * get_vision_capability_first reflects the persisted value (bool-coerced),
  * a missing/null ``agent:`` block is created rather than crashing,
  * round-trip identity across set→get→set cycles.
"""

import importlib
from pathlib import Path

import pytest
import yaml

config = importlib.import_module("api.config")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "agent:\n"
        "  image_input_mode: auto\n"
        "  reasoning_effort: high\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    return cfg_path


def _read_agent(cfg_path: Path) -> dict:
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return data.get("agent") or {}


def test_set_persists_and_preserves_sibling_keys(isolated_config):
    result = config.set_vision_capability_first(True)

    assert result == {"vision_capability_first": True}
    agent = _read_agent(isolated_config)
    assert agent["vision_capability_first"] is True
    # Unrelated keys in the same agent: block must survive the write.
    assert agent["image_input_mode"] == "auto"
    assert agent["reasoning_effort"] == "high"


def test_set_false_flips_persisted_value(isolated_config):
    config.set_vision_capability_first(True)
    config.set_vision_capability_first(False)

    assert _read_agent(isolated_config)["vision_capability_first"] is False
    assert config.get_vision_capability_first() == {"vision_capability_first": False}


def test_get_reflects_absent_key_as_false(isolated_config):
    # No vision_capability_first key written yet.
    assert config.get_vision_capability_first() == {"vision_capability_first": False}


def test_set_creates_agent_block_when_missing_or_null(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  default: x\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)

    config.set_vision_capability_first(True)

    agent = _read_agent(cfg_path)
    assert agent["vision_capability_first"] is True
    assert cfg_path.read_text(encoding="utf-8").rstrip().endswith("vision_capability_first: true")


def test_set_coerces_truthiness(isolated_config):
    # The route validates bool at the API boundary; the setter double-guards.
    config.set_vision_capability_first(True)
    assert config.get_vision_capability_first()["vision_capability_first"] is True
    config.set_vision_capability_first(0)
    assert config.get_vision_capability_first()["vision_capability_first"] is False
