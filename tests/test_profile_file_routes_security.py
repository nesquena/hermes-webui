import io
import json
import sys
import types
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _body(handler: _FakeHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _patch_profile_resolver(monkeypatch, profile_home):
    fake_profiles = types.ModuleType("api.profiles")
    fake_profiles._resolve_profile_home_for_name = lambda name: profile_home
    monkeypatch.setitem(sys.modules, "api.profiles", fake_profiles)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        monkeypatch.setattr(api_pkg, "profiles", fake_profiles, raising=False)


def test_profile_file_read_allows_intended_env_contents(monkeypatch, tmp_path):
    import api.routes as routes

    assert ".env" in routes._PROFILE_FILE_WHITELIST

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    _patch_profile_resolver(monkeypatch, profile_home)

    handler = _FakeHandler()
    routes._handle_profile_file_read(
        handler,
        urlparse("/api/profile/files?name=test&file=.env"),
    )
    payload = _body(handler)

    assert handler.status == 200
    assert payload.get("redacted") is not True, payload
    assert payload["content"] == "OPENAI_API_KEY=sk-secret\n"


def test_profile_file_write_allows_intended_config_yaml(monkeypatch, tmp_path):
    import api.routes as routes

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    config_path = profile_home / "config.yaml"
    config_path.write_text("providers:\n  openai:\n    api_key: old-secret\n", encoding="utf-8")
    _patch_profile_resolver(monkeypatch, profile_home)

    handler = _FakeHandler()
    routes._handle_profile_file_write(
        handler,
        {"name": "test", "file": "config.yaml", "content": "model:\n  default: changed\n"},
    )
    payload = _body(handler)

    assert handler.status == 200
    assert payload["ok"] is True
    assert config_path.read_text(encoding="utf-8") == "model:\n  default: changed\n"
