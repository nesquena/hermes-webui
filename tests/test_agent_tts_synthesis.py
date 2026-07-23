"""Bounded Agent synthesis and descriptor-bound audio validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from api import agent_tts, agent_tts_worker
from api.profiles import build_profile_subprocess_env


def _adts(payload=b"abc"):
    frame_length = 7 + len(payload)
    return bytes(
        [
            0xFF,
            0xF1,
            0x50,
            0x80 | ((frame_length >> 11) & 0x03),
            (frame_length >> 3) & 0xFF,
            ((frame_length & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    ) + payload


def _mp3_frame():
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417-byte frame.
    return b"\xff\xfb\x90\x64" + b"\x00" * 413


def _ogg_page(payload=b"OpusHead\x01\x02\x00\x00\x00\x00\x00\x00\x00"):
    return (
        b"OggS\x00\x02"
        + b"\x00" * 8
        + b"\x01\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + b"\x01"
        + bytes([len(payload)])
        + payload
    )


def _flac_stream():
    return b"fLaC\x80\x00\x00\x22" + b"\x00" * 34 + b"\xff\xf8\x00\x00\x00\x00"


def _wave_file(payload=b"data"):
    fmt = b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00"
    chunks = b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    chunks += b"data" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WAVE" + chunks


VALID_AUDIO = {
    "speech.mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + _mp3_frame(),
    "speech.ogg": _ogg_page(),
    "speech.wav": _wave_file(),
    "speech.flac": _flac_stream(),
    "speech.aac": _adts(),
    "speech-adif.aac": b"ADIF\x00\x00\x00\x01" + b"audioaac",
}
EXPECTED_MIME = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


@pytest.mark.parametrize(("filename", "body"), VALID_AUDIO.items())
def test_descriptor_validator_accepts_supported_signatures(tmp_path, filename, body):
    artifact = tmp_path / filename
    artifact.write_bytes(body)

    audio = agent_tts._consume_audio_artifact(
        {"artifact_path": str(artifact), "provider": "fixture"}, tmp_path
    )

    assert audio.data == body
    assert audio.content_type == EXPECTED_MIME[artifact.suffix]
    assert audio.provider == "fixture"


@pytest.mark.parametrize(
    ("filename", "body"),
    [
        ("empty.mp3", b""),
        ("fake.mp3", b"not-mp3"),
        ("truncated.aac", b"\xff\xf1\x50"),
        ("bad-length.aac", _adts(b"x")[:-1]),
        ("container.m4a", b"\x00\x00\x00\x18ftypM4A "),
        ("mismatch.wav", VALID_AUDIO["speech.mp3"]),
        ("mismatch.mp3", VALID_AUDIO["speech.wav"]),
    ],
)
def test_descriptor_validator_rejects_empty_malformed_and_mismatch(
    tmp_path, filename, body
):
    artifact = tmp_path / filename
    artifact.write_bytes(body)

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts._consume_audio_artifact(
            {"artifact_path": str(artifact), "provider": "fixture"}, tmp_path
        )

    assert exc_info.value.code == "tts_artifact_invalid"


@pytest.mark.parametrize(
    "body",
    [
        b"ID3\x04\x00\x00\x00\x00\x00\x00",
        b"\xff\xe1\x00\x00",
        b"\xff\xff\x00\x00",
        b"OggS\x00\x02" + b"\x00" * 21,
        b"fLaC\x00\x00\x00\x22",
        b"RIFF\x08\x00\x00\x00WAVEdata",
        b"ADIF\x00\x00\x00\x00",
    ],
)
def test_descriptor_validator_rejects_signature_only_non_audio(tmp_path, body):
    artifact = tmp_path / "speech.mp3"
    artifact.write_bytes(body)

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts._consume_audio_artifact(
            {"artifact_path": str(artifact), "provider": "fixture"}, tmp_path
        )

    assert exc_info.value.code == "tts_artifact_invalid"


def test_descriptor_validator_rejects_escape_symlink_fifo_and_oversize(
    tmp_path, monkeypatch
):
    outside = tmp_path.parent / "outside.mp3"
    outside.write_bytes(VALID_AUDIO["speech.mp3"])
    with pytest.raises(agent_tts.AgentTtsError):
        agent_tts._consume_audio_artifact(
            {"artifact_path": str(outside), "provider": "x"}, tmp_path
        )

    target = tmp_path / "target.mp3"
    target.write_bytes(VALID_AUDIO["speech.mp3"])
    link = tmp_path / "link.mp3"
    link.symlink_to(target)
    with pytest.raises(agent_tts.AgentTtsError):
        agent_tts._consume_audio_artifact(
            {"artifact_path": str(link), "provider": "x"}, tmp_path
        )

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "audio.mp3"
        os.mkfifo(fifo)
        with pytest.raises(agent_tts.AgentTtsError):
            agent_tts._consume_audio_artifact(
                {"artifact_path": str(fifo), "provider": "x"}, tmp_path
            )

    huge = tmp_path / "huge.mp3"
    huge.write_bytes(VALID_AUDIO["speech.mp3"])
    monkeypatch.setattr(agent_tts, "AGENT_TTS_MAX_AUDIO_BYTES", 8)
    with pytest.raises(agent_tts.AgentTtsError) as too_large:
        agent_tts._consume_audio_artifact(
            {"artifact_path": str(huge), "provider": "x"}, tmp_path
        )
    assert too_large.value.code == "tts_artifact_too_large"


class FakeTts:
    def __init__(self, body, *, actual_provider=None, limit=20, available=True):
        self.body = body
        self.actual_provider = actual_provider
        self.limit = limit
        self.available = available
        self.load_calls = 0
        self.snapshots_seen = []
        self._load_tts_config = self._changing_load
        self._get_provider = lambda cfg: cfg.get("provider", "edge")

    def _changing_load(self):
        self.load_calls += 1
        return {"provider": "changed-externally"}

    def check_tts_requirements(self):
        self.snapshots_seen.append(self._load_tts_config())
        return self.available

    def _resolve_max_text_length(self, provider, config):
        self.snapshots_seen.append(dict(config))
        return self.limit

    def _check_edge_available(self):
        return self.actual_provider != "neutts"

    def _check_neutts_available(self):
        return self.actual_provider == "neutts"

    def text_to_speech_tool(self, text, output_path):
        snapshot = self._load_tts_config()
        self.snapshots_seen.append(snapshot)
        path = Path(output_path)
        path.write_bytes(self.body)
        return json.dumps(
            {
                "success": True,
                "file_path": str(path),
                "provider": self.actual_provider or snapshot["provider"],
            }
        )


def _install_worker(monkeypatch, tmp_path, tts, config=None):
    config = config or {"tts": {"provider": "edge", "voice": "fixed"}}
    loads = []

    def load_config():
        loads.append(True)
        return json.loads(json.dumps(config))

    cfg = SimpleNamespace(load_config=load_config)
    tools = SimpleNamespace()
    monkeypatch.setattr(
        agent_tts_worker, "_import_agent_modules", lambda: (tts, tools, cfg)
    )
    return loads


def test_worker_uses_one_immutable_snapshot_and_exact_agent_call(monkeypatch, tmp_path):
    tts = FakeTts(VALID_AUDIO["speech.mp3"], limit=5)
    loads = _install_worker(monkeypatch, tmp_path, tts)
    output = tmp_path / "audio.mp3"

    result = agent_tts_worker.synthesize_payload("hello", output, tmp_path)

    assert loads == [True]
    assert tts.load_calls == 0
    assert all(row["provider"] == "edge" for row in tts.snapshots_seen)
    assert result["artifact_path"] == str(output)
    assert result["provider"] == "edge"


def test_worker_accepts_only_explicit_edge_to_neutts_fallback(monkeypatch, tmp_path):
    tts = FakeTts(
        VALID_AUDIO["speech.mp3"], actual_provider="neutts", available=True
    )
    _install_worker(monkeypatch, tmp_path, tts)

    result = agent_tts_worker.synthesize_payload(
        "hello", tmp_path / "audio.mp3", tmp_path
    )
    assert result["provider"] == "neutts"
    assert result["configured_provider"] == "edge"

    tts.actual_provider = "openai"
    with pytest.raises(agent_tts_worker.WorkerOperationError) as mismatch:
        agent_tts_worker.synthesize_payload(
            "hello", tmp_path / "audio2.mp3", tmp_path
        )
    assert mismatch.value.code == "provider_mismatch"


def test_worker_enforces_exact_dynamic_limit_and_availability(monkeypatch, tmp_path):
    tts = FakeTts(VALID_AUDIO["speech.mp3"], limit=2)
    _install_worker(monkeypatch, tmp_path, tts)
    agent_tts_worker.synthesize_payload("🙂🙂", tmp_path / "ok.mp3", tmp_path)

    with pytest.raises(agent_tts_worker.WorkerOperationError) as too_long:
        agent_tts_worker.synthesize_payload("🙂🙂🙂", tmp_path / "long.mp3", tmp_path)
    assert too_long.value.code == "text_too_long"

    tts.available = False
    with pytest.raises(agent_tts_worker.WorkerOperationError) as unavailable:
        agent_tts_worker.synthesize_payload("ok", tmp_path / "no.mp3", tmp_path)
    assert unavailable.value.code == "provider_unavailable"


def test_worker_accepts_distinct_returned_artifact_only_inside_request_root(
    monkeypatch, tmp_path
):
    tts = FakeTts(VALID_AUDIO["speech.wav"])
    _install_worker(monkeypatch, tmp_path, tts)

    def distinct(text, output_path):
        path = tmp_path / "provider-output.wav"
        path.write_bytes(VALID_AUDIO["speech.wav"])
        return json.dumps({"success": True, "file_path": str(path), "provider": "edge"})

    tts.text_to_speech_tool = distinct
    result = agent_tts_worker.synthesize_payload(
        "hello", tmp_path / "audio.mp3", tmp_path
    )
    assert result["artifact_path"].endswith("provider-output.wav")

    outside = tmp_path.parent / "escape.wav"

    def escaped(text, output_path):
        outside.write_bytes(VALID_AUDIO["speech.wav"])
        return json.dumps({"success": True, "file_path": str(outside), "provider": "edge"})

    tts.text_to_speech_tool = escaped
    with pytest.raises(agent_tts_worker.WorkerOperationError) as exc_info:
        agent_tts_worker.synthesize_payload(
            "hello", tmp_path / "audio.mp3", tmp_path
        )
    assert exc_info.value.code == "tts_artifact_invalid"


def test_public_synthesis_consumes_artifact_before_request_cleanup(tmp_path, monkeypatch):
    scope = agent_tts.AgentTtsProfileScope(
        "voice",
        tmp_path / "profile",
        tmp_path / "profile" / "config.yaml",
        {"HERMES_HOME": str(tmp_path / "profile")},
    )

    class Popen:
        pid = 1234
        returncode = None

        def __init__(self, command, **kwargs):
            pass

        def communicate(self, payload, timeout):
            request = json.loads(payload)
            artifact = Path(request["request_dir"]) / "speech.ogg"
            artifact.write_bytes(VALID_AUDIO["speech.ogg"])
            agent_tts_worker.write_status_file(
                Path(request["status_path"]),
                {
                    "schema": 1,
                    "ok": True,
                    "code": "ok",
                    "artifact_path": str(artifact),
                    "provider": "edge",
                },
            )
            self.returncode = 0
            return None, None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(agent_tts.subprocess, "Popen", Popen)

    audio = agent_tts.synthesize_agent_tts(
        "hello", profile_scope=scope, owner_key="user:voice"
    )

    assert audio.content_type == "audio/ogg"
    assert audio.data == VALID_AUDIO["speech.ogg"]
    request_root = scope.home / "cache" / "webui-tts-requests"
    assert not list(request_root.glob("request-*"))


def test_real_installed_agent_offline_command_provider(tmp_path):
    profile_home = tmp_path / "offline-profile"
    profile_home.mkdir()
    writer = tmp_path / "write_wav.py"
    writer.write_text(
        """
import sys, wave
with wave.open(sys.argv[1], 'wb') as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(16000)
    output.writeframes(b'fixture-audio')
""",
        encoding="utf-8",
    )
    command = (
        f'"{agent_tts.PYTHON_EXE}" "{writer}" '
        "{output_path}"
    )
    config = {
        "tts": {
            "provider": "webui-offline-fixture",
            "providers": {
                "webui-offline-fixture": {
                    "type": "command",
                    "command": command,
                    "output_format": "wav",
                    "max_text_length": 100,
                }
            },
        }
    }
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    scope = agent_tts.AgentTtsProfileScope(
        name="offline-fixture",
        home=profile_home,
        config_path=profile_home / "config.yaml",
        child_env=build_profile_subprocess_env(
            "offline-fixture", profile_home
        ),
    )

    audio = agent_tts.synthesize_agent_tts(
        "offline integration",
        profile_scope=scope,
        owner_key="integration:offline-fixture",
    )

    assert audio.content_type == "audio/wav"
    assert audio.provider == "webui-offline-fixture"
    assert audio.data.startswith(b"RIFF")
    assert not list(
        (profile_home / "cache" / "webui-tts-requests").glob("request-*")
    )
