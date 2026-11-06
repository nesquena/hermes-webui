"""WebUI unlock endpoints for external password managers (Bitwarden / 1Password).

Pins: the routes are wired, the master password is only handed to the backend's
unlock() and never echoed back (even inside a backend error), unknown backends and
empty passwords are rejected, lock goes through the agent's session store, and the
frontend control is loaded, translated, and never persists the password.
"""

import io
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "vault_unlock.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self, body: dict | None = None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


class _FakeBackend:
    name = "bitwarden"
    display_name = "Bitwarden"
    needs_unlock = True

    def __init__(self, fail_with=None):
        self.received = []
        self.unlocked = False
        self.fail_with = fail_with

    def is_unlocked(self):
        return self.unlocked

    def unlock(self, master):
        self.received.append(master)
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with.format(pw=master))
        self.unlocked = True


@pytest.fixture
def fake_vault(monkeypatch):
    """Install a fake agent.vault_backends package; no real manager CLI is touched."""
    state = {"backend": _FakeBackend(), "locked": []}
    pkg = types.ModuleType("agent.vault_backends")
    pkg.enabled_backends = lambda: [state["backend"]]
    sess = types.ModuleType("agent.vault_backends.unlock")
    sess.lock = lambda name=None: state["locked"].append(name)
    pkg.unlock = sess
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.vault_backends", pkg)
    monkeypatch.setitem(sys.modules, "agent.vault_backends.unlock", sess)
    homes = types.ModuleType("hermes_constants")
    homes.set_hermes_home_override = lambda home: state.setdefault("homes", []).append(home) or "tok"
    homes.reset_hermes_home_override = lambda tok: None
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: homes)
    return state


def _get(path):
    from api.routes import handle_get

    h = _FakeHandler()
    handle_get(h, urlparse("http://example.com" + path))
    return h


def _post(path, body):
    from api.routes import handle_post

    h = _FakeHandler(body)
    handle_post(h, urlparse("http://example.com" + path))
    return h


def test_status_lists_unlockable_backends(fake_vault):
    h = _get("/api/vault/status")
    assert h.status == 200
    assert h.json_body()["profile"]
    assert h.json_body()["backends"] == [
        {"name": "bitwarden", "display_name": "Bitwarden", "unlocked": False}
    ]


def test_unlock_passes_password_to_backend_only(fake_vault):
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": "s3cret-pw"})
    assert h.status == 200
    assert h.json_body() == {"success": True, "backend": "bitwarden", "unlocked": True}
    assert fake_vault["backend"].received == ["s3cret-pw"]
    assert b"s3cret-pw" not in bytes(h.body)


def test_unlock_error_never_echoes_password(fake_vault):
    fake_vault["backend"] = _FakeBackend(fail_with="bad password {pw}")
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": "s3cret-pw"})
    assert h.status == 400
    assert h.json_body()["success"] is False
    assert b"s3cret-pw" not in bytes(h.body)


def test_unlock_rejects_unknown_backend(fake_vault):
    h = _post("/api/vault/unlock", {"backend": "nope", "master_password": "x"})
    assert h.status == 404
    assert fake_vault["backend"].received == []


def test_unlock_rejects_empty_password(fake_vault):
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": ""})
    assert h.status == 400
    assert fake_vault["backend"].received == []


def test_lock_uses_agent_session_store(fake_vault):
    h = _post("/api/vault/lock", {"backend": "bitwarden"})
    assert h.status == 200
    assert fake_vault["locked"] == ["bitwarden"]


def test_frontend_is_loaded_and_translated():
    assert 'src="static/vault_unlock.js?v=__WEBUI_VERSION__"' in INDEX_HTML
    for key in ("vault_unlock_title", "vault_master_password", "vault_unlock_btn", "vault_lock_btn"):
        assert key in JS and key in I18N


def test_frontend_never_persists_password():
    for sink in ("localStorage", "sessionStorage", "document.cookie", "console."):
        assert sink not in JS
    assert "input.value = ''" in JS


def test_status_is_profile_scoped(fake_vault):
    from api.profiles import get_active_hermes_home

    _get("/api/vault/status")
    assert fake_vault["homes"] == [str(get_active_hermes_home())]


def test_fails_closed_without_profile_home_override(fake_vault, monkeypatch):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: None)
    assert _get("/api/vault/status").json_body() == {"backends": []}
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": "x"})
    assert h.status == 501
    assert fake_vault["backend"].received == []


def test_unlock_error_is_fixed_even_when_truncation_would_split_password(fake_vault):
    # A long backend message that places the password across any truncation
    # boundary must not leak a prefix of it: the error text is fixed.
    pw = "s3cret-password-value"
    fake_vault["backend"] = _FakeBackend(fail_with="x" * 290 + "{pw}")
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": pw})
    assert h.status == 400
    assert h.json_body() == {"success": False, "error": "Unlock failed"}
    assert b"s3cret" not in bytes(h.body)


def test_actions_from_a_stale_profile_panel_are_rejected(fake_vault):
    current = _get("/api/vault/status").json_body()["profile"]
    h = _post("/api/vault/lock", {"backend": "bitwarden", "profile": current + "-old"})
    assert h.status == 409
    assert fake_vault["locked"] == []
    h = _post("/api/vault/unlock", {"backend": "bitwarden", "master_password": "pw", "profile": current + "-old"})
    assert h.status == 409
    assert fake_vault["backend"].received == []
    h = _post("/api/vault/lock", {"backend": "bitwarden", "profile": current})
    assert h.status == 200
    assert fake_vault["locked"] == ["bitwarden"]


def test_lock_without_vault_module_is_501_not_500(fake_vault, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.vault_backends", None)
    monkeypatch.setitem(sys.modules, "agent.vault_backends.unlock", None)
    h = _post("/api/vault/lock", {"backend": "bitwarden"})
    assert h.status == 501
    assert _get("/api/vault/status").json_body()["backends"] == []


def test_frontend_drops_panel_on_profile_change():
    # The panel is rebuilt from fresh status, closed when the reported profile
    # changes, and every action carries the profile it was rendered for.
    assert "if (p !== profile) closePanel();" in JS
    assert "refresh().then(openPanel)" in JS
    assert "profile: profile" in JS
    assert "S.activeProfile" in JS


def test_standalone_titlebar_keeps_lock_in_flow():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".pwa-standalone .app-titlebar .vault-unlock-btn{position:static" in css
