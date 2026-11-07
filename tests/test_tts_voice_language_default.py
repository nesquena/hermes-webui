"""Language-aware Edge voice defaults for /api/tts.

Covers the three behaviours the fix adds, plus the two regressions a review
reproduced on the first head:

* an omitted voice is picked from the text's language (it -> Elsa, en -> Ava);
* the configured default is read through the profile-expanded loader, so a
  ``${VAR}`` reference resolves and an unresolved one degrades instead of
  reaching the allowlist literally;
* the lookup happens INSIDE the Edge branch — an OpenAI/ElevenLabs request and
  a playback-profile rejection never read Edge (or any profile) config.

Same in-process fake-handler pattern as test_issue2931_edge_tts_endpoint.py;
edge_tts is stubbed, so nothing here touches the network.
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, command: str = "POST", headers=None, client="1.2.3.4"):
        self.command = command
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = headers or {}
        self.headers.setdefault("Content-Length", str(len(body)))
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except Exception:
            return None


def _post(body_dict, **kw):
    return _FakeHandler(json.dumps(body_dict).encode(), **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    import api.auth as _auth

    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def _stub_edge_tts(monkeypatch, captured):
    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice

        def stream_sync(self):
            yield {"type": "audio", "data": b"abc"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))


def _patch_profile_config(monkeypatch, cfg):
    """Serve ``cfg`` from the same loader the endpoint must use."""
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "load_profile_config", lambda home, env=None: cfg)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: "/tmp/fake-profile")


# ── language detection ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ciao, come stai oggi? Questo è un test della voce italiana.", "it"),
        ("The build is green and I will merge it now.", "en"),
        ("Fatto, riavviato il servizio e ora è attivo.", "it"),
        ("Ok", ""),
        ("Sì", ""),
        ("", ""),
    ],
)
def test_detect_text_lang(text, expected):
    assert routes._detect_text_lang(text) == expected


# ── default voice resolution ──────────────────────────────────────────────


def test_omitted_voice_is_italian_for_italian_text(monkeypatch):
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "it-IT-ElsaNeural"}}})
    assert routes._default_edge_voice("Questo è un test con abbastanza parole.") == "it-IT-ElsaNeural"


def test_omitted_voice_is_english_for_english_text(monkeypatch):
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "it-IT-ElsaNeural"}}})
    assert routes._default_edge_voice("This is a test with enough words here.") == "en-US-AvaNeural"


def test_per_language_config_wins_over_builtin(monkeypatch):
    cfg = {"tts": {"edge": {"voices": {"it": "it-IT-IsabellaNeural"}}}}
    _patch_profile_config(monkeypatch, cfg)
    assert routes._default_edge_voice("Questo è un test con abbastanza parole.") == "it-IT-IsabellaNeural"


def test_unresolved_env_reference_is_treated_as_absent(monkeypatch):
    # load_profile_config leaves an unresolvable ${VAR} literal; forwarding it
    # would trip the allowlist, so it must degrade to the built-in default.
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "${REVIEW_VOICE}"}}})
    assert routes._default_edge_voice("Ok") == "it-IT-ElsaNeural"


@pytest.mark.parametrize("cfg", [{}, {"tts": "edge"}, {"tts": {"edge": True}}, {"tts": {"edge": {"voice": 7}}}])
def test_malformed_config_shapes_never_crash(monkeypatch, cfg):
    _patch_profile_config(monkeypatch, cfg)
    assert routes._default_edge_voice("Ok") == "it-IT-ElsaNeural"


# ── handler: ordering regressions ─────────────────────────────────────────


def _fail_if_called(*_a, **_kw):
    raise AssertionError("Edge voice resolution ran where it must not")


def test_openai_request_never_reads_edge_config(monkeypatch):
    monkeypatch.setattr(routes, "_default_edge_voice", _fail_if_called)
    h = _post({"text": "hello", "engine": "openai"}, client="10.42.0.1")
    routes._handle_tts(h, None)
    # No OpenAI key in the test env: the request stops at the credential check.
    assert h.status == 503


def test_elevenlabs_request_never_reads_edge_config(monkeypatch):
    monkeypatch.setattr(routes, "_default_edge_voice", _fail_if_called)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    h = _post({"text": "hello", "engine": "elevenlabs"}, client="10.42.0.2")
    routes._handle_tts(h, None)
    assert h.status in (503,)


def test_profile_mismatch_is_409_without_reading_config(monkeypatch):
    monkeypatch.setattr(routes, "_default_edge_voice", _fail_if_called)
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "_profiles_match", lambda a, b: False)
    h = _post({"text": "hello", "profile": "beta"}, client="10.42.0.3")
    routes._handle_tts(h, None)
    assert h.status == 409


# ── handler: end-to-end default selection ─────────────────────────────────


def test_omitted_voice_synthesizes_in_italian(monkeypatch):
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "it-IT-ElsaNeural"}}})
    captured = {}
    _stub_edge_tts(monkeypatch, captured)
    h = _post({"text": "Questo è un test con abbastanza parole italiane."}, client="10.42.1.1")
    routes._handle_tts(h, None)
    assert h.status == 200, h.payload()
    assert captured["voice"] == "it-IT-ElsaNeural"


def test_omitted_voice_synthesizes_in_english(monkeypatch):
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "it-IT-ElsaNeural"}}})
    captured = {}
    _stub_edge_tts(monkeypatch, captured)
    h = _post({"text": "This is a test with enough English words."}, client="10.42.1.2")
    routes._handle_tts(h, None)
    assert h.status == 200, h.payload()
    assert captured["voice"] == "en-US-AvaNeural"


def test_explicit_voice_override_still_wins(monkeypatch):
    _patch_profile_config(monkeypatch, {"tts": {"edge": {"voice": "it-IT-ElsaNeural"}}})
    captured = {}
    _stub_edge_tts(monkeypatch, captured)
    h = _post(
        {"text": "This is a test with enough English words.", "voice": "fr-FR-DeniseNeural"},
        client="10.42.1.3",
    )
    routes._handle_tts(h, None)
    assert h.status == 200, h.payload()
    assert captured["voice"] == "fr-FR-DeniseNeural"
