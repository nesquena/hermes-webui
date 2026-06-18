import base64
import io
import json
import hashlib
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes

from cryptography.hazmat.primitives.asymmetric import ec


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def cbor(obj):
    if isinstance(obj, int):
        if obj >= 0:
            return _cbor_int(0, obj)
        return _cbor_int(1, -1 - obj)
    if isinstance(obj, bytes):
        return _cbor_int(2, len(obj)) + obj
    if isinstance(obj, str):
        raw = obj.encode()
        return _cbor_int(3, len(raw)) + raw
    if isinstance(obj, list):
        return _cbor_int(4, len(obj)) + b"".join(cbor(x) for x in obj)
    if isinstance(obj, dict):
        return _cbor_int(5, len(obj)) + b"".join(cbor(k) + cbor(v) for k, v in obj.items())
    if obj is None:
        return b"\xf6"
    if obj is False:
        return b"\xf4"
    if obj is True:
        return b"\xf5"
    raise TypeError(obj)


def _cbor_int(major, value):
    prefix = major << 5
    if value < 24:
        return bytes([prefix | value])
    if value < 256:
        return bytes([prefix | 24, value])
    if value < 65536:
        return bytes([prefix | 25]) + value.to_bytes(2, "big")
    return bytes([prefix | 26]) + value.to_bytes(4, "big")


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeHandler:
    headers = FakeHeaders({"Host": "localhost:8787"})


def _set_paths(monkeypatch, tmp_path):
    import api.passkeys as passkeys
    monkeypatch.setattr(passkeys, "_CREDENTIALS_FILE", tmp_path / "passkeys.json")
    monkeypatch.setattr(passkeys, "_CHALLENGES_FILE", tmp_path / ".passkey_challenges.json")
    return passkeys


def _client_data(kind, challenge, origin="http://localhost:8787"):
    raw = json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()
    return raw, b64u(raw)


def test_passkey_registration_stores_public_credential_metadata(monkeypatch, tmp_path):
    passkeys = _set_paths(monkeypatch, tmp_path)
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_numbers()
    credential_id = b"credential-1"
    cose_key = {1: 2, 3: -7, -1: 1, -2: pub.x.to_bytes(32, "big"), -3: pub.y.to_bytes(32, "big")}
    opts = passkeys.registration_options(FakeHandler())
    client_raw, client_b64 = _client_data("webauthn.create", opts["challenge"])
    auth_data = (
        hashlib.sha256(opts["rp"]["id"].encode()).digest()
        + bytes([0x41])
        + (1).to_bytes(4, "big")
        + (b"\0" * 16)
        + len(credential_id).to_bytes(2, "big")
        + credential_id
        + cbor(cose_key)
    )
    attestation = cbor({"fmt": "none", "authData": auth_data, "attStmt": {}})

    result = passkeys.finish_registration({
        "label": "MacBook Touch ID",
        "response": {"clientDataJSON": client_b64, "attestationObject": b64u(attestation)},
    }, FakeHandler())

    assert result["ok"] is True
    creds = passkeys.registered_credentials()
    assert creds == [{
        "id": b64u(credential_id),
        "label": "MacBook Touch ID",
        "created_at": creds[0]["created_at"],
        "last_used_at": None,
        "sign_count": 1,
    }]
    assert "public_key_pem" not in creds[0]


def test_passkey_login_verifies_signature_and_updates_usage(monkeypatch, tmp_path):
    passkeys = _set_paths(monkeypatch, tmp_path)
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_numbers()
    credential_id = b"credential-2"
    cose_key = {1: 2, 3: -7, -1: 1, -2: pub.x.to_bytes(32, "big"), -3: pub.y.to_bytes(32, "big")}

    reg_opts = passkeys.registration_options(FakeHandler())
    _raw, client_b64 = _client_data("webauthn.create", reg_opts["challenge"])
    reg_auth_data = hashlib.sha256(reg_opts["rp"]["id"].encode()).digest() + bytes([0x41]) + (1).to_bytes(4, "big") + (b"\0" * 16) + len(credential_id).to_bytes(2, "big") + credential_id + cbor(cose_key)
    passkeys.finish_registration({"response": {"clientDataJSON": client_b64, "attestationObject": b64u(cbor({"fmt": "none", "authData": reg_auth_data, "attStmt": {}}))}}, FakeHandler())

    login_opts = passkeys.authentication_options(FakeHandler())
    client_raw, login_client_b64 = _client_data("webauthn.get", login_opts["challenge"])
    login_auth_data = hashlib.sha256(login_opts["rpId"].encode()).digest() + bytes([0x01]) + (2).to_bytes(4, "big")
    signature = key.sign(login_auth_data + hashlib.sha256(client_raw).digest(), ec.ECDSA(hashes.SHA256()))

    result = passkeys.finish_login({
        "id": b64u(credential_id),
        "response": {
            "clientDataJSON": login_client_b64,
            "authenticatorData": b64u(login_auth_data),
            "signature": b64u(signature),
        },
    }, FakeHandler())

    assert result == {"ok": True, "credential_id": b64u(credential_id)}
    [cred] = passkeys.registered_credentials()
    assert cred["sign_count"] == 2
    assert cred["last_used_at"] is not None


def test_passkey_options_evict_oldest_challenges_per_context(monkeypatch, tmp_path):
    passkeys = _set_paths(monkeypatch, tmp_path)
    passkeys._save_credentials([{"id": b64u(b"credential-3"), "label": "This device"}])
    monkeypatch.setattr(passkeys, "_MAX_CHALLENGES_PER_CONTEXT", 2)

    first = passkeys.authentication_options(FakeHandler())
    second = passkeys.authentication_options(FakeHandler())
    third = passkeys.authentication_options(FakeHandler())

    pending = json.loads((tmp_path / ".passkey_challenges.json").read_text(encoding="utf-8"))
    assert set(pending) == {second["challenge"], third["challenge"]}
    assert first["challenge"] not in pending


def test_passkey_options_evict_oldest_challenges_globally(monkeypatch, tmp_path):
    passkeys = _set_paths(monkeypatch, tmp_path)
    passkeys._save_credentials([{"id": b64u(b"credential-4"), "label": "This device"}])
    monkeypatch.setattr(passkeys, "_MAX_CHALLENGES", 2)
    monkeypatch.setattr(passkeys, "_MAX_CHALLENGES_PER_CONTEXT", 10)

    first = passkeys.authentication_options(FakeHandler())
    second = passkeys.authentication_options(FakeHandler())
    third = passkeys.authentication_options(FakeHandler())

    pending = json.loads((tmp_path / ".passkey_challenges.json").read_text(encoding="utf-8"))
    assert set(pending) == {second["challenge"], third["challenge"]}
    assert first["challenge"] not in pending


class RouteFakeHandler:
    def __init__(self):
        self.headers = FakeHeaders({"Host": "localhost:8787", "Content-Length": "0"})
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.client_address: tuple[str, int] = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_passkey_options_rate_limit_errors_return_429(monkeypatch):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)

    def raise_rate_limit(_handler):
        raise passkeys.PasskeyRateLimitError("too many")

    monkeypatch.setattr(passkeys, "authentication_options", raise_rate_limit)
    handler = RouteFakeHandler()

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/options"))

    assert handler.status == 429
    assert json.loads(handler.wfile.getvalue())["error"] == "too many"


def test_passkey_register_options_handles_base_passkey_errors(monkeypatch):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "session")
    monkeypatch.setattr(auth, "verify_session", lambda cookie: True)

    def raise_passkey_error(_handler):
        raise passkeys.PasskeyError("plain passkey error")

    monkeypatch.setattr(passkeys, "registration_options", raise_passkey_error)
    handler = RouteFakeHandler()

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/register/options"))

    assert handler.status == 400
    assert json.loads(handler.wfile.getvalue())["error"] == "plain passkey error"

def test_first_passkey_registration_options_rejects_remote_bootstrap(monkeypatch, tmp_path):
    import api.auth as auth
    import api.routes as routes

    _set_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)

    handler = RouteFakeHandler()
    handler.client_address = ("8.8.8.8", 12345)
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/register/options"))

    assert handler.status == 401
    assert json.loads(handler.wfile.getvalue())["error"] == "Authentication required"


def test_first_passkey_registration_rejects_remote_bootstrap(monkeypatch, tmp_path):
    import api.auth as auth
    import api.routes as routes

    _set_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)

    handler = RouteFakeHandler()
    handler.client_address = ("8.8.8.8", 12345)
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/register"))

    assert handler.status == 401
    assert json.loads(handler.wfile.getvalue())["error"] == "Authentication required"


def test_first_passkey_registration_options_allows_local_bootstrap(monkeypatch, tmp_path):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    _set_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(passkeys, "registration_options", lambda handler: {"challenge": "local-bootstrap"})

    handler = RouteFakeHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/register/options"))

    assert handler.status == 200
    assert json.loads(handler.wfile.getvalue())["publicKey"] == {"challenge": "local-bootstrap"}


def test_first_passkey_registration_allows_local_bootstrap(monkeypatch, tmp_path):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    _set_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(passkeys, "finish_registration", lambda body, handler: {"ok": True})
    monkeypatch.setattr(passkeys, "registered_credentials", lambda: [{"id": "local-bootstrap"}])

    handler = RouteFakeHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/passkey/register"))

    assert handler.status == 200
    assert json.loads(handler.wfile.getvalue()) == {"ok": True, "credentials": [{"id": "local-bootstrap"}]}


def test_passkey_registration_requires_valid_session_when_auth_is_enabled(monkeypatch):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: None)
    monkeypatch.setattr(
        auth,
        "verify_session",
        lambda cookie: (_ for _ in ()).throw(AssertionError("verify_session should not be called without a cookie")),
    )
    monkeypatch.setattr(
        passkeys,
        "registration_options",
        lambda handler: (_ for _ in ()).throw(AssertionError("registration_options should not be reached")),
    )
    monkeypatch.setattr(
        passkeys,
        "finish_registration",
        lambda body, handler: (_ for _ in ()).throw(AssertionError("finish_registration should not be reached")),
    )

    for path in ("/api/auth/passkey/register/options", "/api/auth/passkey/register"):
        handler = RouteFakeHandler()
        routes.handle_post(handler, SimpleNamespace(path=path))

        assert handler.status == 401
        assert json.loads(handler.wfile.getvalue())["error"] == "Authentication required"


def test_auth_status_reports_passkey_availability_source_contract():
    src = open("api/routes.py", encoding="utf-8").read()
    assert '"passkeys_enabled"' in src
    assert '"passkeys_count"' in src
    assert '"password_auth_enabled"' in src
    assert '"passwordless_enabled"' in src
    assert 'registered_credentials()' in src


def test_login_page_has_default_hidden_passkey_button_and_script_wiring():
    routes = open("api/routes.py", encoding="utf-8").read()
    login_js = open("static/login.js", encoding="utf-8").read()
    assert 'id="passkey-login"' in routes
    assert 'style="display:none"' in routes
    assert "api/auth/passkey/options" in login_js
    assert "navigator.credentials.get" in login_js


def test_passwordless_mode_keeps_auth_enabled_with_passkeys(monkeypatch, tmp_path):
    import api.auth as auth
    # Stage-batch14: passkey support is opt-in default-off behind HERMES_WEBUI_PASSKEY=1
    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    passkeys = _set_paths(monkeypatch, tmp_path)
    passkeys._save_credentials([{"id": "cred-1", "label": "This device"}])
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)

    assert auth.are_passkeys_enabled() is True
    assert auth.is_auth_enabled() is True


def test_passkey_feature_flag_off_disables_passkeys_even_with_credentials(monkeypatch, tmp_path):
    """When HERMES_WEBUI_PASSKEY is unset/0, are_passkeys_enabled() returns False."""
    import api.auth as auth
    passkeys = _set_paths(monkeypatch, tmp_path)
    passkeys._save_credentials([{"id": "cred-1", "label": "This device"}])
    monkeypatch.delenv("HERMES_WEBUI_PASSKEY", raising=False)
    monkeypatch.setattr(auth, "get_config", lambda: {}, raising=False)
    assert auth.are_passkeys_enabled() is False


def test_passkey_feature_flag_via_config(monkeypatch, tmp_path):
    """webui_passkey_enabled: true in config also enables the surface."""
    import api.auth as auth
    passkeys = _set_paths(monkeypatch, tmp_path)
    passkeys._save_credentials([{"id": "cred-1", "label": "This device"}])
    monkeypatch.delenv("HERMES_WEBUI_PASSKEY", raising=False)
    # Patch the config import inside _passkey_feature_flag_enabled
    import api.config
    monkeypatch.setattr(api.config, "get_config", lambda: {"webui_passkey_enabled": True})
    assert auth.are_passkeys_enabled() is True


def test_passwordless_settings_and_last_passkey_guard_are_wired():
    routes = open("api/routes.py", encoding="utf-8").read()
    panels = open("static/panels.js", encoding="utf-8").read()
    index = open("static/index.html", encoding="utf-8").read()

    assert "_passwordless" in routes
    assert "Register a passkey before going passwordless." in routes
    assert "Set a password or disable auth before removing the last passkey." in routes
    assert "clear_credentials()" in routes
    assert "id=\"btnGoPasswordless\"" in index
    assert "async function goPasswordless" in panels
    assert "prompt(" not in panels
