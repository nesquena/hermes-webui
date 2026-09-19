"""Codex-native context usage must not masquerade as a Hermes 75% countdown."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("compression", "expected"),
    [
        ({}, "native"),
        ({"codex_app_server_auto": "hermes"}, "hermes"),
        ({"codex_app_server_auto": "off"}, "off"),
        ({"codex_app_server_auto": "invalid"}, "native"),
    ],
)
def test_settings_surface_validated_codex_auto_compaction_mode(
    monkeypatch, compression, expected
):
    import api.config as config

    monkeypatch.setattr(config, "_read_raw_settings_file", lambda: {})
    monkeypatch.setattr(config, "get_effective_default_model", lambda: "gpt-test")
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"model": {"provider": "openai-codex"}, "compression": compression},
    )

    settings = config.load_settings()

    assert settings["codex_app_server_auto"] == expected
    assert settings["default_model_provider"] == "openai-codex"


def test_codex_auto_mode_is_read_only_and_never_persisted_to_webui_settings():
    import api.config as config

    persisted = config._settings_payload_for_write(
        {"theme": "dark", "codex_app_server_auto": "native"}, set()
    )

    assert persisted == {"theme": "dark"}


def test_boot_hydrates_codex_auto_compaction_mode():
    assert "window._codexAppServerAutoCompaction=" in BOOT_JS
    assert "? s.codex_app_server_auto" in BOOT_JS
    assert ": 'native';" in BOOT_JS


def test_native_mode_is_provider_scoped_and_not_a_75_percent_promise():
    assert "function _isCodexNativeContext()" in UI_JS
    assert "provider==='openai-codex'" in UI_JS
    assert "Compaction managed automatically by Codex" in UI_JS
    assert "Hermes local estimate:" in UI_JS
    assert "const compressText=nativeManaged?'':" in UI_JS


def test_native_mode_removes_warning_colours_but_keeps_visible_ring():
    assert "el.classList.toggle('ctx-native',nativeManaged);" in UI_JS
    assert "!nativeManaged&&pct>75" in UI_JS
    assert ".ctx-indicator.ctx-native .ctx-ring-value{stroke:var(--accent);}" in STYLE_CSS
