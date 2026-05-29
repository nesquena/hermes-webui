"""Tests for the Voice module (multi-model, OpenAI-compatible STT).

Covers api.upload.handle_voice_transcribe and the config + history APIs in
api.voice_transcription. The composer-dictation endpoint (handle_transcribe)
is covered separately in test_voice_transcribe_endpoint.py.
"""
import io
import json

import urllib.request
import pytest

import api.voice_transcription as voice
from api.upload import handle_voice_transcribe


def _multipart_body(fields=None, files=None, boundary=b"voiceboundary"):
    fields = fields or {}
    files = files or {}
    body = b""
    for name, value in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for name, (filename, data, content_type) in files.items():
        body += b"--" + boundary + b"\r\n"
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode()
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return body, f"multipart/form-data; boundary={boundary.decode()}"


class _FakeHandler:
    def __init__(self, body: bytes, content_type: str):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _FakeResp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Isolate config + history files and clear env fallback."""
    monkeypatch.setattr(voice, "VOICE_CONFIG_FILE", tmp_path / "voice_config.json")
    monkeypatch.setattr(voice, "VOICE_TRANSCRIPTS_FILE", tmp_path / "voice_transcripts.json")
    monkeypatch.delenv("GRANITE_STT_BASE_URL", raising=False)
    return tmp_path


def _model(**kw):
    base = {
        "id": "granite",
        "label": "Granite 2B",
        "base_url": "http://localhost:8000/v1",
        "model": "granite-speech-3.3-2b",
        "timeout": 90,
    }
    base.update(kw)
    return base


# ── Config CRUD ──────────────────────────────────────────────────────────────
def test_save_and_public_config_masks_keys(isolated):
    voice.save_voice_config([_model(api_key="secret-token")], active_id="granite")
    pub = voice.public_config()
    assert pub["active_id"] == "granite"
    assert pub["configured"] is True
    m = pub["models"][0]
    assert m["has_api_key"] is True
    assert "api_key" not in m  # never leaked


def test_omitted_api_key_is_preserved(isolated):
    voice.save_voice_config([_model(api_key="keep-me")], active_id="granite")
    # Re-save without api_key (UI omits unchanged secret).
    voice.save_voice_config([_model()], active_id="granite")
    assert voice.get_model("granite")["api_key"] == "keep-me"
    # Explicit empty string clears it.
    voice.save_voice_config([_model(api_key="")], active_id="granite")
    assert voice.get_model("granite")["api_key"] == ""


def test_invalid_model_rejected(isolated):
    with pytest.raises(ValueError):
        voice.save_voice_config([_model(id="Bad Id!")])
    with pytest.raises(ValueError):
        voice.save_voice_config([_model(base_url="ftp://nope")])
    with pytest.raises(ValueError):
        voice.save_voice_config([_model(model="")])
    with pytest.raises(ValueError):
        voice.save_voice_config([_model(id="env-granite")])  # reserved


def test_active_clamped_to_existing(isolated):
    cfg = voice.save_voice_config([_model(id="a"), _model(id="b")], active_id="ghost")
    assert cfg["active_id"] == "a"  # falls back to first


def test_set_active_model(isolated):
    voice.save_voice_config([_model(id="a"), _model(id="b")], active_id="a")
    cfg = voice.set_active_model("b")
    assert cfg["active_id"] == "b"
    with pytest.raises(ValueError):
        voice.set_active_model("nope")


def test_env_fallback_model(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "VOICE_CONFIG_FILE", tmp_path / "voice_config.json")
    monkeypatch.setenv("GRANITE_STT_BASE_URL", "http://localhost:9000/v1")
    pub = voice.public_config()
    assert pub["configured"] is True
    env = pub["models"][0]
    assert env["id"] == "env-granite"
    assert env["source"] == "env"
    # Env model is not persisted to disk.
    voice.save_voice_config([_model(id="user1")], active_id="user1")
    on_disk = json.loads((tmp_path / "voice_config.json").read_text())
    assert [m["id"] for m in on_disk["models"]] == ["user1"]


# ── Transcribe endpoint ──────────────────────────────────────────────────────
def test_transcribe_unconfigured_returns_503(isolated):
    body, ct = _multipart_body(files={"file": ("voice.webm", b"RIFFfake", "audio/webm")})
    handler = _FakeHandler(body, ct)
    handle_voice_transcribe(handler)
    assert handler.status == 503
    assert "configured" in handler.payload()["error"].lower()


def test_transcribe_success_uses_selected_model_and_persists(isolated, monkeypatch):
    voice.save_voice_config(
        [_model(id="a", model="model-a"), _model(id="b", model="model-b")],
        active_id="a",
    )
    captured = {}

    def fake_open(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeResp(json.dumps({"text": "hello world"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)

    # Request model b explicitly via model_id field.
    body, ct = _multipart_body(
        fields={"model_id": "b"},
        files={"file": ("voice.webm", b"RIFFfake", "audio/webm")},
    )
    handler = _FakeHandler(body, ct)
    handle_voice_transcribe(handler)

    assert handler.status == 200
    p = handler.payload()
    assert p["ok"] is True and p["transcript"] == "hello world"
    assert p["model_id"] == "b"
    assert captured["url"].endswith("/audio/transcriptions")
    assert b"model-b" in captured["data"]  # routed to model b's name
    assert captured["timeout"] == 90

    # History persisted with model id.
    hist = voice.load_transcripts()
    assert hist[0]["text"] == "hello world"
    assert hist[0]["model_id"] == "b"


def test_transcribe_unknown_model_id(isolated, monkeypatch):
    voice.save_voice_config([_model(id="a")], active_id="a")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"{}"))
    body, ct = _multipart_body(
        fields={"model_id": "ghost"},
        files={"file": ("voice.webm", b"RIFFfake", "audio/webm")},
    )
    handler = _FakeHandler(body, ct)
    handle_voice_transcribe(handler)
    assert handler.status == 400
    assert "not found" in handler.payload()["error"].lower()


# ── History CRUD ─────────────────────────────────────────────────────────────
def test_history_crud(isolated):
    a = voice.add_transcript("first")
    b = voice.add_transcript("second")
    assert [h["text"] for h in voice.load_transcripts()] == ["second", "first"]
    assert voice.add_transcript("   ") is None
    assert voice.delete_transcript(a["id"]) is True
    assert voice.delete_transcript("nope") is False
    assert [h["text"] for h in voice.load_transcripts()] == ["second"]
    voice.clear_transcripts()
    assert voice.load_transcripts() == []
    assert b["id"] != a["id"]
