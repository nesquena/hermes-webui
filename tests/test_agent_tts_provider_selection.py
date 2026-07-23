"""Agent/Desktop-parity provider selection and compensation tests."""

from __future__ import annotations

import copy
import os
import threading
import time
from pathlib import Path

import pytest
import yaml

from api import agent_tts_worker, config as webui_config


class FakeTts:
    def __init__(self, available=None):
        self.available = available or {"edge": True, "openai": True}
        self._load_tts_config = lambda: {}
        self._get_provider = lambda cfg: cfg.get("provider", "edge")
        self.text_to_speech_tool = lambda text, output_path: None
        self._resolve_max_text_length = lambda provider, cfg: 4000

    def check_tts_requirements(self):
        provider = str(self._load_tts_config().get("provider", "edge"))
        return bool(self.available.get(provider, False))


class FakeTools:
    def __init__(self):
        self.rows = [
            {
                "name": "Nous Subscription",
                "tts_provider": "openai",
                "managed_nous_feature": "tts",
            },
            {"name": "OpenAI TTS", "tts_provider": "openai"},
            {"name": "Microsoft Edge TTS", "tts_provider": "edge"},
        ]
        self.TOOL_CATEGORIES = {
            "tts": {"name": "Text-to-Speech", "providers": self.rows}
        }

    def _visible_providers(self, category, config, *, force_fresh=False):
        return list(self.rows)

    def _is_provider_active(self, row, config, *, force_fresh=False):
        tts = config.get("tts", {})
        return tts.get("provider", "edge") == row["tts_provider"] and bool(
            tts.get("use_gateway", False)
        ) == bool(row.get("managed_nous_feature"))

    def apply_provider_selection(self, toolset, provider_name, config):
        row = next(row for row in self.rows if row["name"] == provider_name)
        tts = config.setdefault("tts", {})
        tts["provider"] = row["tts_provider"]
        tts["use_gateway"] = bool(row.get("managed_nous_feature"))

    def provider_readiness_status(self, row, config, **kwargs):
        return "ready"


class FakeConfig:
    def __init__(self, path: Path, config: dict, *, refuse_save=False):
        self.path = path
        self.current = copy.deepcopy(config)
        self.saved = []
        self.refuse_save = refuse_save

    def get_config_path(self):
        return self.path

    def load_config(self):
        return copy.deepcopy(self.current)

    def save_config(self, config):
        self.saved.append(copy.deepcopy(config))
        if self.refuse_save:
            return
        self.current = copy.deepcopy(config)
        target = Path(self.get_config_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately replaces its argument path like an old writer. The worker
        # must bind this call to the symlink referent.
        temp = target.with_name(target.name + ".agent-tmp")
        temp.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        os.replace(temp, target)


class DiskConfig(FakeConfig):
    """Fake Agent config module that observes every authoritative disk write."""

    def load_config(self):
        target = Path(self.get_config_path())
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        self.current = loaded if isinstance(loaded, dict) else {}
        return copy.deepcopy(self.current)


def _install(monkeypatch, path, config, *, available=None, refuse_save=False):
    tts = FakeTts(available)
    tools = FakeTools()
    cfg = FakeConfig(path, config, refuse_save=refuse_save)
    monkeypatch.setattr(
        agent_tts_worker, "_import_agent_modules", lambda: (tts, tools, cfg)
    )
    return tts, tools, cfg


def _install_disk(monkeypatch, path, config):
    tts = FakeTts()
    tools = FakeTools()
    cfg = DiskConfig(path, config)
    monkeypatch.setattr(
        agent_tts_worker, "_import_agent_modules", lambda: (tts, tools, cfg)
    )
    return cfg


def _fingerprint(config):
    return agent_tts_worker._config_fingerprint(config)


@pytest.mark.parametrize("symlinked", [False, True])
def test_concurrent_webui_write_conflicts_then_retry_preserves_unrelated_keys(
    tmp_path, monkeypatch, symlinked
):
    target = tmp_path / "real" / "config.yaml"
    target.parent.mkdir()
    original = {
        "model": {"default": "before/model"},
        "tts": {"provider": "edge"},
    }
    target.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")
    path = target
    link_inode = None
    if symlinked:
        link_dir = tmp_path / "link"
        link_dir.mkdir()
        path = link_dir / "config.yaml"
        path.symlink_to(target)
        link_inode = os.lstat(path).st_ino

    _install_disk(monkeypatch, path, original)
    writer_has_lock = threading.Event()
    release_writer = threading.Event()

    def mutate(current):
        current.setdefault("model", {})["default"] = "after/model"
        writer_has_lock.set()
        assert release_writer.wait(timeout=5)

    writer = threading.Thread(
        target=lambda: webui_config.update_yaml_config_file(path, mutate), daemon=True
    )
    writer.start()
    assert writer_has_lock.wait(timeout=5)

    result = {}

    def select_with_stale_fingerprint():
        try:
            agent_tts_worker.select_provider_payload(
                "OpenAI TTS", "openai", _fingerprint(original)
            )
        except agent_tts_worker.WorkerOperationError as exc:
            result["error"] = exc.code

    selector = threading.Thread(target=select_with_stale_fingerprint, daemon=True)
    selector.start()
    time.sleep(0.05)
    assert selector.is_alive(), "provider write must wait for the WebUI config lock"
    release_writer.set()
    writer.join(timeout=5)
    selector.join(timeout=5)
    assert not writer.is_alive()
    assert not selector.is_alive()
    assert result == {"error": "config_conflict"}

    after_webui = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert after_webui["model"]["default"] == "after/model"
    assert after_webui["tts"]["provider"] == "edge"

    agent_tts_worker.select_provider_payload(
        "OpenAI TTS", "openai", _fingerprint(after_webui)
    )
    final = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert final["model"]["default"] == "after/model"
    assert final["tts"]["provider"] == "openai"
    if symlinked:
        assert path.is_symlink()
        assert os.lstat(path).st_ino == link_inode


def test_selects_exact_duplicate_provider_row_and_preserves_unrelated_config(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    original = {
        "model": {"default": "example/model"},
        "stt": {"provider": "whisper"},
        "tts": {"provider": "edge", "edge": {"voice": "old"}, "speed": 1.2},
    }
    _tts, _tools, cfg = _install(monkeypatch, path, original)

    result = agent_tts_worker.select_provider_payload(
        "Nous Subscription", "openai", _fingerprint(original)
    )

    assert len(cfg.saved) == 1
    assert cfg.current["tts"] == {
        "provider": "openai",
        "use_gateway": True,
        "edge": {"voice": "old"},
        "speed": 1.2,
    }
    assert cfg.current["model"] == original["model"]
    assert cfg.current["stt"] == original["stt"]
    assert result["active_provider_name"] == "Nous Subscription"
    assert result["config_fingerprint"] == _fingerprint(cfg.current)


def test_byok_exact_row_clears_managed_gateway_and_updates_legacy_voice_atomically(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    original = {"tts": {"provider": "openai", "use_gateway": True}}
    _tts, _tools, cfg = _install(monkeypatch, path, original)

    agent_tts_worker.select_provider_payload(
        "Microsoft Edge TTS",
        "edge",
        _fingerprint(original),
        legacy_edge_voice="en-US-AvaNeural",
    )

    assert len(cfg.saved) == 1
    assert cfg.current["tts"] == {
        "provider": "edge",
        "use_gateway": False,
        "edge": {"voice": "en-US-AvaNeural"},
    }


def test_empty_legacy_voice_is_noop(tmp_path, monkeypatch):
    original = {"tts": {"provider": "edge", "edge": {"voice": "keep"}}}
    _tts, _tools, cfg = _install(monkeypatch, tmp_path / "config.yaml", original)

    agent_tts_worker.select_provider_payload(
        "Microsoft Edge TTS", "edge", _fingerprint(original), legacy_edge_voice=""
    )

    assert cfg.current["tts"]["edge"]["voice"] == "keep"


@pytest.mark.parametrize(
    ("name", "provider_id", "expected_code"),
    [
        ("Unknown", "edge", "invalid_provider"),
        ("Microsoft Edge TTS", "openai", "invalid_provider"),
    ],
)
def test_unknown_or_mismatched_row_fails_before_save(
    tmp_path, monkeypatch, name, provider_id, expected_code
):
    original = {"tts": {"provider": "edge"}}
    _tts, _tools, cfg = _install(monkeypatch, tmp_path / "config.yaml", original)

    with pytest.raises(agent_tts_worker.WorkerOperationError) as exc_info:
        agent_tts_worker.select_provider_payload(
            name, provider_id, _fingerprint(original)
        )

    assert exc_info.value.code == expected_code
    assert cfg.saved == []


def test_stale_fingerprint_and_unavailable_candidate_fail_before_save(
    tmp_path, monkeypatch
):
    original = {"tts": {"provider": "edge"}}
    _tts, _tools, cfg = _install(
        monkeypatch,
        tmp_path / "config.yaml",
        original,
        available={"edge": True, "openai": False},
    )

    with pytest.raises(agent_tts_worker.WorkerOperationError) as stale:
        agent_tts_worker.select_provider_payload(
            "OpenAI TTS", "openai", "sha256:stale"
        )
    assert stale.value.code == "config_conflict"

    with pytest.raises(agent_tts_worker.WorkerOperationError) as unavailable:
        agent_tts_worker.select_provider_payload(
            "OpenAI TTS", "openai", _fingerprint(original)
        )
    assert unavailable.value.code == "provider_unavailable"
    assert cfg.saved == []


def test_read_only_or_managed_save_is_detected_by_authoritative_reprobe(
    tmp_path, monkeypatch
):
    original = {"tts": {"provider": "edge"}}
    _tts, _tools, cfg = _install(
        monkeypatch, tmp_path / "config.yaml", original, refuse_save=True
    )

    with pytest.raises(agent_tts_worker.WorkerOperationError) as exc_info:
        agent_tts_worker.select_provider_payload(
            "OpenAI TTS", "openai", _fingerprint(original)
        )

    assert exc_info.value.code == "config_write_failed"
    assert len(cfg.saved) == 1
    assert cfg.current == original


def test_symlinked_config_binds_old_writer_to_referent(tmp_path, monkeypatch):
    target_dir = tmp_path / "profile-real"
    link_dir = tmp_path / "profile-link"
    target_dir.mkdir()
    link_dir.mkdir()
    target = target_dir / "config.yaml"
    target.write_text("tts:\n  provider: edge\n", encoding="utf-8")
    link = link_dir / "config.yaml"
    link.symlink_to(target)
    inode = os.lstat(link).st_ino
    original = {"tts": {"provider": "edge"}}
    _tts, _tools, cfg = _install(monkeypatch, link, original)

    agent_tts_worker.select_provider_payload(
        "OpenAI TTS", "openai", _fingerprint(original)
    )

    assert link.is_symlink()
    assert os.lstat(link).st_ino == inode
    assert link.resolve() == target.resolve()
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["tts"]["provider"] == "openai"


def test_compensation_requires_expected_post_fingerprint_and_restores_only_tts(
    tmp_path, monkeypatch
):
    prior = {"model": {"default": "x"}, "tts": {"provider": "edge"}}
    _tts, _tools, cfg = _install(monkeypatch, tmp_path / "config.yaml", prior)
    selected = agent_tts_worker.select_provider_payload(
        "OpenAI TTS", "openai", _fingerprint(prior)
    )
    post = copy.deepcopy(cfg.current)

    restored = agent_tts_worker.restore_tts_payload(
        selected["previous_tts"],
        selected["previous_tts_present"],
        selected["config_fingerprint"],
    )

    assert restored["active_provider"] == "edge"
    assert cfg.current["tts"] == prior["tts"]
    assert cfg.current["model"] == prior["model"]
    assert len(cfg.saved) == 2

    selected_again = agent_tts_worker.select_provider_payload(
        "OpenAI TTS", "openai", restored["config_fingerprint"]
    )
    cfg.current["model"]["default"] = "changed-externally"
    with pytest.raises(agent_tts_worker.WorkerOperationError) as conflict:
        agent_tts_worker.restore_tts_payload(
            selected_again["previous_tts"],
            True,
            selected_again["config_fingerprint"],
        )
    assert conflict.value.code == "config_conflict"
    assert cfg.current["tts"] == post["tts"]
    assert cfg.current["model"]["default"] == "changed-externally"
    assert len(cfg.saved) == 3
