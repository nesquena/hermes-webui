"""#8909: the STT capability probe must find the Agent's ffmpeg resolver in both layouts.

Older agents define ``_find_ffmpeg_binary`` on ``tools.transcription_tools``. Current agents
moved it to ``tools.transcription_audio`` without re-exporting it, so the probe reported
local-command STT as unavailable while ``/api/transcribe`` used it, and the mic fell back to
browser speech recognition.
"""

import sys
import types

import pytest

from api.upload import _stt_provider_capability_from_module


def _stt_module(provider="local", *, enabled=True, with_helper=False, helper=None):
    mod = types.ModuleType("tools.transcription_tools")
    mod.__dict__.update(
        {
            "_load_stt_config": lambda: {"provider": provider},
            "is_stt_enabled": lambda cfg=None: enabled,
            "_HAS_FASTER_WHISPER": False,
            "_HAS_OPENAI": False,
            "_HAS_MISTRAL": False,
            "_has_local_command": lambda: True,
        }
    )
    if with_helper:
        mod.__dict__["_find_ffmpeg_binary"] = helper
    return mod


def _audio_module(monkeypatch, helper):
    audio = types.ModuleType("tools.transcription_audio")
    audio.__dict__["_find_ffmpeg_binary"] = helper
    monkeypatch.setitem(sys.modules, "tools.transcription_audio", audio)
    return audio


@pytest.mark.parametrize("provider", ["local", "local_command"])
def test_current_layout_resolver_makes_local_command_available(monkeypatch, provider):
    _audio_module(monkeypatch, lambda: "/usr/bin/ffmpeg")
    assert _stt_provider_capability_from_module(_stt_module(provider)) == (True, "local_command")


def test_current_layout_without_ffmpeg_stays_unavailable(monkeypatch):
    _audio_module(monkeypatch, lambda: None)
    assert _stt_provider_capability_from_module(_stt_module("local"))[0] is False
    assert _stt_provider_capability_from_module(_stt_module("local_command"))[0] is False


def test_resolver_in_neither_layout_means_no_converter(monkeypatch):
    # A None entry makes ``from tools.transcription_audio import ...`` raise ImportError.
    monkeypatch.setitem(sys.modules, "tools.transcription_audio", None)
    assert _stt_provider_capability_from_module(_stt_module("local"))[0] is False


def test_resolver_that_raises_means_no_converter(monkeypatch):
    def boom():
        raise OSError("probe failed")

    _audio_module(monkeypatch, boom)
    assert _stt_provider_capability_from_module(_stt_module("local"))[0] is False


def test_disabled_stt_stays_unavailable_with_a_converter(monkeypatch):
    _audio_module(monkeypatch, lambda: "/usr/bin/ffmpeg")
    assert _stt_provider_capability_from_module(_stt_module("local", enabled=False))[0] is False


def test_older_layout_resolver_on_the_module_still_decides(monkeypatch):
    # The module's own resolver wins; the submodule is only a fallback.
    _audio_module(monkeypatch, lambda: "/usr/bin/ffmpeg")
    old = _stt_module("local", with_helper=True, helper=lambda: None)
    assert _stt_provider_capability_from_module(old)[0] is False
    old_ok = _stt_module("local", with_helper=True, helper=lambda: "/usr/local/bin/ffmpeg")
    assert _stt_provider_capability_from_module(old_ok) == (True, "local_command")


def test_installed_agent_local_command_is_reported_available(monkeypatch, tmp_path):
    """With the real Agent modules, the probe agrees with the Agent's own local-command choice."""
    stt = pytest.importorskip("tools.transcription_tools")
    if hasattr(stt, "_find_ffmpeg_binary"):
        resolver_home = stt  # older layout
    else:
        resolver_home = pytest.importorskip("tools.transcription_audio")
        if not hasattr(resolver_home, "_find_ffmpeg_binary"):
            pytest.skip("installed Agent has the ffmpeg resolver in neither known layout")
    command = tmp_path / "stt.sh"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    monkeypatch.setenv("HERMES_LOCAL_STT_COMMAND", f"{command} {{input_path}} {{output_dir}} {{language}}")
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", False)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {"provider": "local"})
    monkeypatch.setattr(stt, "is_stt_enabled", lambda cfg=None: True, raising=False)
    if not stt._has_local_command():
        pytest.skip("installed Agent does not read HERMES_LOCAL_STT_COMMAND")

    monkeypatch.setattr(resolver_home, "_find_ffmpeg_binary", lambda: "/opt/test/ffmpeg")
    assert _stt_provider_capability_from_module(stt) == (True, "local_command")

    monkeypatch.setattr(resolver_home, "_find_ffmpeg_binary", lambda: None)
    assert _stt_provider_capability_from_module(stt)[0] is False
