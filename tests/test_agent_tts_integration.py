"""Real WebUI -> worker -> installed Agent TTS integration coverage."""

from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from tests.conftest import requires_agent
from api import agent_tts, routes


pytestmark = requires_agent


class _Handler:
    def __init__(self, text: str, *, client: str = "127.0.0.1"):
        body = json.dumps({"engine": "agent", "text": text}).encode("utf-8")
        self.command = "POST"
        self.path = "/api/tts"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body))}
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


def _writer(tmp_path: Path) -> Path:
    path = tmp_path / "write_fixture.py"
    path.write_text(
        """
import pathlib, sys, time
fmt, sentinel, output = sys.argv[1:]
time.sleep(0.05)
marker = sentinel.encode('utf-8')
mp3 = b'ID3\\x04\\x00\\x00\\x00\\x00\\x00\\x00\\xff\\xfb\\x90\\x64' + b'\\x00' * 413
ogg_payload = b'OpusHead\\x01\\x02\\x00\\x00\\x00\\x00\\x00\\x00\\x00' + marker
parts = {
    'mp3': mp3 + marker,
    'ogg': b'OggS\\x00\\x02' + b'\\x00' * 16 + b'\\x00' * 4 + b'\\x01' + bytes([len(ogg_payload)]) + ogg_payload,
    'flac': b'fLaC\\x80\\x00\\x00\\x22' + b'\\x00' * 34 + b'\\xff\\xf8\\x00\\x00\\x00\\x00' + marker,
}
if fmt == 'wav':
    import io, wave
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(marker)
    data = buffer.getvalue()
else:
    data = parts[fmt]
pathlib.Path(output).write_bytes(data)
""",
        encoding="utf-8",
    )
    return path


def _scope(tmp_path: Path, name: str, fmt: str, sentinel: str, writer: Path):
    home = tmp_path / name
    home.mkdir(parents=True)
    command = (
        f'"{agent_tts.PYTHON_EXE}" "{writer}" {fmt} {sentinel} '
        "{output_path}"
    )
    config = {
        "tts": {
            "provider": f"fixture-{name}",
            "providers": {
                f"fixture-{name}": {
                    "type": "command",
                    "command": command,
                    "output_format": fmt,
                    "max_text_length": 100,
                }
            },
        }
    }
    config_path = home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_CONFIG_PATH": str(config_path),
            "HERMES_SESSION_PLATFORM": "webui",
            "WEBUI_TTS_PROFILE_SENTINEL": sentinel,
        }
    )
    return agent_tts.AgentTtsProfileScope(name, home, config_path, env)


@pytest.mark.parametrize(
    ("fmt", "mime", "prefix"),
    [
        ("mp3", "audio/mpeg", b"ID3"),
        ("wav", "audio/wav", b"RIFF"),
        ("ogg", "audio/ogg", b"OggS"),
        ("flac", "audio/flac", b"fLaC"),
    ],
)
def test_real_route_worker_agent_returns_raw_fixture_bytes(
    tmp_path, monkeypatch, fmt, mime, prefix
):
    writer = _writer(tmp_path)
    scope = _scope(tmp_path, f"route-{fmt}", fmt, f"sentinel-{fmt}", writer)
    monkeypatch.setattr(routes, "capture_agent_tts_profile_scope", lambda: scope)
    monkeypatch.setattr(routes, "_tts_synthesis_limiter", routes._TtsRateLimiter(0))
    handler = _Handler(f"real {fmt} integration", client=f"127.0.0.{len(fmt)}")

    routes._handle_tts(handler, None)

    assert handler.status == 200
    assert handler.sent_headers["Content-Type"] == mime
    assert handler.sent_headers["Content-Length"] == str(len(handler.wfile.getvalue()))
    assert handler.sent_headers["Cache-Control"] == "no-store"
    assert handler.sent_headers["X-Content-Type-Options"] == "nosniff"
    assert handler.wfile.getvalue().startswith(prefix)
    assert f"sentinel-{fmt}".encode() in handler.wfile.getvalue()
    assert not list((scope.home / "cache" / "webui-tts-requests").glob("request-*"))


def test_real_default_and_named_profiles_are_concurrent_and_isolated(tmp_path):
    writer = _writer(tmp_path)
    default = _scope(tmp_path, "default", "wav", "DEFAULT_ONLY", writer)
    named = _scope(tmp_path, "named", "ogg", "NAMED_ONLY", writer)

    def synthesize(scope, owner):
        return agent_tts.synthesize_agent_tts(
            "parallel profile test", profile_scope=scope, owner_key=owner
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        default_future = pool.submit(synthesize, default, "integration:default")
        named_future = pool.submit(synthesize, named, "integration:named")
        default_audio = default_future.result(timeout=90)
        named_audio = named_future.result(timeout=90)

    assert default_audio.content_type == "audio/wav"
    assert b"DEFAULT_ONLY" in default_audio.data
    assert b"NAMED_ONLY" not in default_audio.data
    assert named_audio.content_type == "audio/ogg"
    assert b"NAMED_ONLY" in named_audio.data
    assert b"DEFAULT_ONLY" not in named_audio.data
    for scope in (default, named):
        assert not list((scope.home / "cache" / "webui-tts-requests").glob("request-*"))


def test_real_global_worker_limit_allows_two_and_rejects_third(tmp_path):
    writer = _writer(tmp_path)
    scopes = [
        _scope(tmp_path, f"limit-{index}", "mp3", f"LIMIT_{index}", writer)
        for index in range(3)
    ]

    def synthesize(index):
        try:
            return agent_tts.synthesize_agent_tts(
                "global saturation",
                profile_scope=scopes[index],
                owner_key=f"integration:limit:{index}",
            )
        except agent_tts.AgentTtsError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(synthesize, range(3)))

    audio = [value for value in results if isinstance(value, agent_tts.AgentTtsAudio)]
    errors = [value for value in results if isinstance(value, agent_tts.AgentTtsError)]
    assert len(audio) == 2
    assert [error.code for error in errors] == ["agent_busy"]
    assert all(result.data.startswith(b"ID3") for result in audio)


def test_real_agent_provider_selection_preserves_symlink_and_agent_reloads_result(
    tmp_path,
):
    real_home = tmp_path / "real-profile"
    link_home = tmp_path / "linked-profile"
    real_home.mkdir()
    link_home.mkdir()
    target = real_home / "config.yaml"
    target.write_text("tts:\n  provider: fixture-old\n", encoding="utf-8")
    link = link_home / "config.yaml"
    link.symlink_to(target)
    link_inode = os.lstat(link).st_ino

    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(link_home),
            # Deliberately retain the link path: the worker must bind Agent save_config
            # to the referent while separately preserving the original link inode.
            "HERMES_CONFIG_PATH": str(link),
            "HERMES_SESSION_PLATFORM": "webui",
        }
    )
    scope = agent_tts.AgentTtsProfileScope("linked", link_home, link, env)
    capability = agent_tts.run_agent_tts_operation(
        "capability",
        {},
        profile_scope=scope,
        owner_key="integration:link-cap",
        timeout_seconds=20,
    )
    edge = next(row for row in capability["providers"] if row["provider_id"] == "edge")
    assert edge["selectable"] is True

    selected = agent_tts.run_agent_tts_operation(
        "select_provider",
        {
            "provider_name": edge["name"],
            "expected_fingerprint": capability["config_fingerprint"],
        },
        profile_scope=scope,
        owner_key="integration:link-select",
        timeout_seconds=20,
    )

    assert selected["active_provider"] == "edge"
    assert link.is_symlink()
    assert os.lstat(link).st_ino == link_inode
    assert link.resolve() == target.resolve()
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["tts"]["provider"] == "edge"

    reloaded = agent_tts.run_agent_tts_operation(
        "capability",
        {},
        profile_scope=scope,
        owner_key="integration:link-reload",
        timeout_seconds=20,
    )
    assert reloaded["active_provider"] == "edge"
    assert reloaded["config_fingerprint"] == selected["config_fingerprint"]
