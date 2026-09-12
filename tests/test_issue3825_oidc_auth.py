import io
import json
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class RouteFakeHandler:
    def __init__(self):
        self.headers = FakeHeaders({"Host": "localhost:8787"})
        self.request = SimpleNamespace()
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    def header_values(self, name):
        needle = name.lower()
        return [value for key, value in self.sent_headers if key.lower() == needle]

def _ec_jwk(private_key, *, kid="key-1", alg="ES256"):
    numbers = private_key.public_key().public_numbers()
    size = (numbers.curve.key_size + 7) // 8
    import api.auth_oidc as auth_oidc

    return {
        "kid": kid,
        "kty": "EC",
        "alg": alg,
        "crv": "P-256",
        "x": auth_oidc._b64u(numbers.x.to_bytes(size, "big")),
        "y": auth_oidc._b64u(numbers.y.to_bytes(size, "big")),
    }

def _signed_es256_jwt(private_key, header, claims):
    import api.auth_oidc as auth_oidc

    header_b64 = auth_oidc._b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims_b64 = auth_oidc._b64u(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signed = f"{header_b64}.{claims_b64}".encode("ascii")
    der_signature = private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header_b64}.{claims_b64}.{auth_oidc._b64u(raw_signature)}"


def test_oidc_start_redirects_with_pkce_state_and_nonce(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_build_authorization_redirect(request_base_url, next_path):
        captured["request_base_url"] = request_base_url
        captured["next_path"] = next_path
        return (
            "https://idp.example/authorize"
            "?response_type=code"
            "&client_id=webui-client"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A8787%2Fapi%2Fauth%2Foidc%2Fcallback"
            "&scope=openid+profile+email"
            "&state=state-token"
            "&nonce=nonce-token"
            "&code_challenge=challenge-token"
            "&code_challenge_method=S256"
        )

    monkeypatch.setattr(
        "api.auth_oidc.build_authorization_redirect",
        fake_build_authorization_redirect,
    )

    handler = RouteFakeHandler()
    routes.handle_get(
        handler,
        SimpleNamespace(path="/api/auth/oidc/start", query="next=%2Fprojects%3Fview%3Dgrid"),
    )

    assert handler.status == 302
    assert captured == {
        "request_base_url": "http://localhost:8787",
        "next_path": "/projects?view=grid",
    }
    [location] = handler.header_values("Location")
    params = parse_qs(urlparse(location).query)
    assert params["response_type"] == ["code"]
    assert params["state"] == ["state-token"]
    assert params["nonce"] == ["nonce-token"]
    assert params["code_challenge"] == ["challenge-token"]
    assert params["code_challenge_method"] == ["S256"]


def test_oidc_callback_exchanges_code_and_sets_existing_session_cookie(monkeypatch):
    import api.auth as auth
    import api.routes as routes

    captured = {}

    def fake_complete_authorization_code_flow(request_base_url, state, code):
        captured["request_base_url"] = request_base_url
        captured["state"] = state
        captured["code"] = code
        return {"next_path": "/chat/123"}

    monkeypatch.setattr(
        "api.auth_oidc.complete_authorization_code_flow",
        fake_complete_authorization_code_flow,
    )
    monkeypatch.setattr(auth, "create_session", lambda: "session-token.signature")

    handler = RouteFakeHandler()
    routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/auth/oidc/callback",
            query="state=state-token&code=code-token",
        ),
    )

    assert handler.status == 302
    assert captured == {
        "request_base_url": "http://localhost:8787",
        "state": "state-token",
        "code": "code-token",
    }
    assert handler.header_values("Location") == ["/chat/123"]
    cookie_headers = handler.header_values("Set-Cookie")
    assert len(cookie_headers) == 1
    assert auth.COOKIE_NAME in cookie_headers[0]
    assert "session-token.signature" in cookie_headers[0]


def test_oidc_callback_rejects_invalid_state_without_setting_session_cookie(monkeypatch):
    import api.routes as routes
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        "api.auth_oidc.complete_authorization_code_flow",
        lambda *_args: (_ for _ in ()).throw(OIDCAuthError("Invalid OIDC state", status_code=401)),
    )

    handler = RouteFakeHandler()
    routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/auth/oidc/callback",
            query="state=missing-state&code=code-token",
        ),
    )

    assert handler.status == 401
    assert handler.json_body()["error"] == "Invalid OIDC state"
    assert handler.header_values("Set-Cookie") == []


def test_oidc_callback_rejects_allowlist_failure_without_setting_session_cookie(monkeypatch):
    import api.routes as routes
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        "api.auth_oidc.complete_authorization_code_flow",
        lambda *_args: (_ for _ in ()).throw(OIDCAuthError("OIDC identity is not allowed", status_code=403)),
    )

    handler = RouteFakeHandler()
    routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/auth/oidc/callback",
            query="state=state-token&code=code-token",
        ),
    )

    assert handler.status == 403
    assert handler.json_body()["error"] == "OIDC identity is not allowed"
    assert handler.header_values("Set-Cookie") == []


def test_auth_status_reports_oidc_capability_without_regressing_passkey_fields(monkeypatch):
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_oidc_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(
        auth,
        "verify_session",
        lambda _cookie: (_ for _ in ()).throw(AssertionError("verify_session should not run without a cookie")),
    )
    monkeypatch.setattr(passkeys, "registered_credentials", lambda: [])

    handler = RouteFakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/auth/status"))

    assert handler.status == 200
    assert handler.json_body() == {
        "auth_enabled": True,
        "logged_in": False,
        "oidc_enabled": True,
        "password_auth_enabled": False,
        "passwordless_enabled": False,
        "passkeys_enabled": False,
        "passkeys_count": 0,
        "passkey_feature_flag": False,
        "auth_disabled_acknowledged": False,
    }


def test_login_page_renders_absolute_oidc_href_when_enabled(monkeypatch):
    import api.routes as routes

    captured = {}

    monkeypatch.setattr("api.auth_oidc.is_oidc_enabled", lambda: True)
    monkeypatch.setattr(
        routes,
        "t",
        lambda _handler, body, *, content_type=None, **_kwargs: captured.update(
            {"body": body, "content_type": content_type}
        ) or True,
    )

    handler = RouteFakeHandler()
    routes.handle_get(
        handler,
        SimpleNamespace(path="/login", query="next=%2Fworkspace%2Fdemo"),
    )

    assert captured["content_type"] == "text/html; charset=utf-8"
    assert 'href="/api/auth/oidc/start?next=/workspace/demo"' in captured["body"]


def test_login_page_hides_password_controls_when_only_oidc_is_enabled(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr("api.auth_oidc.is_oidc_enabled", lambda: True)
    monkeypatch.setattr("api.auth.get_password_hash", lambda: None)
    monkeypatch.setattr(
        routes,
        "t",
        lambda _handler, body, *, content_type=None, **_kwargs: captured.update(
            {"body": body, "content_type": content_type}
        ) or True,
    )

    handler = RouteFakeHandler()
    routes.handle_get(handler, SimpleNamespace(path="/login", query=""))

    assert 'id="oidc-login"' in captured["body"]
    assert 'id="pw"' not in captured["body"]
    assert 'Enter your password to continue' not in captured["body"]


@pytest.mark.parametrize(
    ("auth_enabled", "password_enabled", "oidc_enabled", "next_path", "expected"),
    [
        (False, False, False, "/workspace", "redirect"),
        (True, True, False, "", "password"),
        (True, False, True, "", "oidc"),
        (True, True, True, "", "password_and_oidc"),
        (True, False, False, "", "passkey"),
    ],
)
def test_login_route_composes_auth_modes(
    monkeypatch, auth_enabled, password_enabled, oidc_enabled, next_path, expected
):
    """Every supported auth mode leaves `/login` with a usable route."""
    import api.routes as routes

    captured = {}
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: auth_enabled)
    monkeypatch.setattr(
        "api.auth.get_password_hash", lambda: "hash" if password_enabled else None
    )
    monkeypatch.setattr("api.auth_oidc.is_oidc_enabled", lambda: oidc_enabled)
    monkeypatch.setattr(
        routes,
        "t",
        lambda _handler, body, *, content_type=None, **_kwargs: captured.update(
            {"body": body, "content_type": content_type}
        )
        or True,
    )

    handler = RouteFakeHandler()
    query = f"next={next_path}" if next_path else ""
    routes.handle_get(handler, SimpleNamespace(path="/login", query=query))

    if expected == "redirect":
        assert handler.status == 302
        assert handler.header_values("Location") == ["/workspace"]
        return

    assert captured["content_type"] == "text/html; charset=utf-8"
    body = captured["body"]
    assert ('id="pw"' in body) is (expected in {"password", "password_and_oidc"})
    assert ('id="oidc-login"' in body) is (expected in {"oidc", "password_and_oidc"})
    if expected == "passkey":
        assert 'id="pw"' not in body
        assert 'id="oidc-login"' not in body
        assert 'id="passkey-login"' in body


def test_settings_hides_disable_auth_action_when_oidc_is_enabled():
    """The action only clears local password auth, never native OIDC."""
    panels_js = (Path(__file__).parent.parent / "static" / "panels.js").read_text(
        encoding="utf-8"
    )

    assert "let _settingsOidcEnabled=false;" in panels_js
    assert "_settingsOidcEnabled=!!authStatus.oidc_enabled;" in panels_js
    assert "disableBtn.style.display=active&&!_settingsOidcEnabled?'':'none';" in panels_js


def test_settings_auth_status_distinguishes_oidc_from_passkey_only():
    """Exercise the pure Settings status mapping rather than source strings."""
    panels_js = (Path(__file__).parent.parent / "static" / "panels.js").read_text(
        encoding="utf-8"
    )
    start = panels_js.index("function _settingsAuthStatusDescriptor(")
    end = panels_js.index("\nfunction _renderSettingsAuthStatus", start)
    descriptor = panels_js[start:end]
    script = (
        descriptor
        + "\nconst cases=["
        + "{auth_enabled:true,password_auth_enabled:false,oidc_enabled:true},"
        + "{auth_enabled:true,password_auth_enabled:false,oidc_enabled:false},"
        + "{auth_enabled:true,password_auth_enabled:true,oidc_enabled:true}];"
        + "console.log(JSON.stringify(cases.map(_settingsAuthStatusDescriptor)));"
    )
    output = subprocess.check_output(["node", "-e", script], text=True)
    statuses = json.loads(output)

    assert statuses == [
        {"labelKey": "auth_status_oidc_only", "cls": "detail-badge ok"},
        {"labelKey": "auth_status_passkey_only", "cls": "detail-badge warn"},
        {"labelKey": "auth_status_password", "cls": "detail-badge ok"},
    ]


def test_oidc_enablement_requires_explicit_allowlist(monkeypatch):
    import api.auth_oidc as auth_oidc

    monkeypatch.delenv("HERMES_WEBUI_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_OIDC_ALLOW_CLAIM", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_OIDC_ALLOW_VALUES", raising=False)
    monkeypatch.setattr(
        auth_oidc,
        "get_config",
        lambda: {
            "webui_oidc": {
                "issuer": "https://issuer.example",
                "client_id": "webui-client",
            }
        },
    )

    assert auth_oidc.is_oidc_enabled() is False

def test_oidc_startup_warning_flags_partial_config(monkeypatch):
    import api.auth as auth

    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: {
            "webui_oidc": {
                "issuer": "https://issuer.example",
                "client_id": "webui-client",
            }
        },
    )

    warning = auth.get_oidc_startup_warning()
    assert warning is not None
    assert "allow_claim" in warning
    assert "allow_values" in warning

def test_oidc_startup_warning_ignores_complete_config(monkeypatch):
    import api.auth as auth

    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: {
            "webui_oidc": {
                "issuer": "https://issuer.example",
                "client_id": "webui-client",
                "allow_claim": "email",
                "allow_values": ["user@example.com"],
            }
        },
    )

    assert auth.get_oidc_startup_warning() is None

def test_validate_id_token_rejects_mismatched_jwk_key_family(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc,
        "_parse_jwt",
        lambda _token: (
            {"alg": "RS256", "kid": "key-1"},
            {
                "iss": "https://issuer.example",
                "aud": "webui-client",
                "exp": 32503680000,
                "nonce": "nonce-token",
                "sub": "user-123",
            },
            b"signed",
            b"signature",
        ),
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_jwks_document",
        lambda _jwks_uri, **_kwargs: {
            "keys": [
                {
                    "kid": "key-1",
                    "kty": "EC",
                    "crv": "P-256",
                    "x": "AQ",
                    "y": "Ag",
                }
            ]
        },
    )

    with pytest.raises(OIDCAuthError, match="did not contain the signing key"):
        auth_oidc._validate_id_token(
            "header.payload.signature",
            client_id="webui-client",
            issuer="https://issuer.example",
            nonce="nonce-token",
            jwks_uri="https://issuer.example/jwks",
        )

def test_validate_id_token_accepts_real_es256_jose_signature(monkeypatch):
    import api.auth_oidc as auth_oidc

    private_key = ec.generate_private_key(ec.SECP256R1())
    token = _signed_es256_jwt(
        private_key,
        {"alg": "ES256", "kid": "key-1"},
        {
            "iss": "https://issuer.example",
            "aud": "webui-client",
            "exp": 32503680000,
            "nonce": "nonce-token",
            "sub": "user-123",
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_jwks_document",
        lambda _jwks_uri, **_kwargs: {"keys": [_ec_jwk(private_key)]},
    )

    claims = auth_oidc._validate_id_token(
        token,
        client_id="webui-client",
        issuer="https://issuer.example",
        nonce="nonce-token",
        jwks_uri="https://issuer.example/jwks",
    )

    assert claims["sub"] == "user-123"

def test_complete_authorization_pins_discovery_to_configured_issuer(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc,
        "_resolve_oidc_config",
        lambda: {
            "issuer": "https://issuer.example",
            "client_id": "webui-client",
            "client_secret": "",
            "redirect_uri": "",
            "scopes": ["openid"],
            "allow_claim": "email",
            "allow_values": ["user@example.com"],
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _issuer: {
            "issuer": "https://evil.example",
            "token_endpoint": "https://issuer.example/token",
            "jwks_uri": "https://issuer.example/jwks",
        },
    )
    auth_oidc._pending_flows.clear()
    auth_oidc._pending_flows["state-token"] = {
        "created_at": time.time(),
        "nonce": "nonce-token",
        "code_verifier": "verifier",
        "next_path": "/",
    }

    with pytest.raises(OIDCAuthError, match="discovery issuer"):
        auth_oidc.complete_authorization_code_flow(
            "http://localhost:8787",
            "state-token",
            "code-token",
        )

def test_validate_id_token_refetches_jwks_once_on_key_miss(monkeypatch):
    import api.auth_oidc as auth_oidc

    old_key = ec.generate_private_key(ec.SECP256R1())
    new_key = ec.generate_private_key(ec.SECP256R1())
    token = _signed_es256_jwt(
        new_key,
        {"alg": "ES256", "kid": "new-key"},
        {
            "iss": "https://issuer.example",
            "aud": "webui-client",
            "exp": 32503680000,
            "nonce": "nonce-token",
            "sub": "user-123",
        },
    )
    jwks_uri = "https://issuer.example/jwks"
    auth_oidc._jwks_cache.clear()
    auth_oidc._jwks_cache[jwks_uri] = (
        time.time() + 300,
        {"keys": [_ec_jwk(old_key, kid="old-key")]},
    )
    fetches = []

    def fake_fetch_json(url):
        fetches.append(url)
        return {"keys": [_ec_jwk(new_key, kid="new-key")]}

    monkeypatch.setattr(auth_oidc, "_fetch_json", fake_fetch_json)

    claims = auth_oidc._validate_id_token(
        token,
        client_id="webui-client",
        issuer="https://issuer.example",
        nonce="nonce-token",
        jwks_uri=jwks_uri,
    )

    assert claims["sub"] == "user-123"
    assert fetches == [jwks_uri]

def test_pending_oidc_flows_are_bounded(monkeypatch):
    import api.auth_oidc as auth_oidc

    monkeypatch.setattr(auth_oidc, "_MAX_PENDING_FLOWS", 2)
    auth_oidc._pending_flows.clear()
    now = time.time()
    auth_oidc._store_pending_flow("old", {"created_at": now - 2, "nonce": "old"})
    auth_oidc._store_pending_flow("middle", {"created_at": now - 1, "nonce": "middle"})
    auth_oidc._store_pending_flow("new", {"created_at": now, "nonce": "new"})

    assert set(auth_oidc._pending_flows) == {"middle", "new"}


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/hostname/.well-known/openid-configuration", "must use https"),
        ("https://127.0.0.1/.well-known/openid-configuration", "private or local addresses"),
    ],
)
def test_fetch_json_rejects_unsafe_oidc_urls(url, message):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    with pytest.raises(OIDCAuthError, match=message):
        auth_oidc._fetch_json(url)


def test_fetch_json_rejects_dns_resolved_private_hosts(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443))
        ],
    )

    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._fetch_json("https://issuer.example/.well-known/openid-configuration")


def test_select_public_key_rejects_wrong_ec_curve_for_alg():
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    private_key = ec.generate_private_key(ec.SECP256R1())
    jwks = {"keys": [_ec_jwk(private_key, alg="ES384")]}

    with pytest.raises(OIDCAuthError, match="did not contain the signing key"):
        auth_oidc._select_public_key(jwks, {"alg": "ES384", "kid": "key-1"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_parse_jwt_rejects_non_finite_numeric_claims(value):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    header = auth_oidc._b64u(b'{"alg":"RS256"}')
    claims = auth_oidc._b64u(
        json.dumps({"exp": value}, separators=(",", ":")).encode("utf-8")
    )
    signature = auth_oidc._b64u(b"signature")
    token = f"{header}.{claims}.{signature}"

    with pytest.raises(OIDCAuthError, match="could not be decoded"):
        auth_oidc._parse_jwt(token)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_coerce_numeric_claim_rejects_non_finite_values(value):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    with pytest.raises(OIDCAuthError, match="claim exp was not numeric"):
        auth_oidc._coerce_numeric_claim({"exp": value}, "exp")


def test_normalize_allow_values_and_scopes_use_separate_delimiters():
    """#6244: allowlist values are comma/newline-delimited (multi-word group names
    like "Hermes Users" stay intact), while OAuth scopes stay space-delimited
    (RFC 6749 §3.3). The two parsers must NOT share whitespace-splitting."""
    from api import auth_oidc

    # Allowlist: multi-word group name stays ONE entry; commas/newlines split.
    assert auth_oidc._normalize_allow_values("Hermes Users") == ["Hermes Users"]
    assert auth_oidc._normalize_allow_values("Hermes Users, Admins") == ["Hermes Users", "Admins"]
    assert auth_oidc._normalize_allow_values("a\nb") == ["a", "b"]
    # Blank collection elements are filtered (a bare [""] must not brick OIDC login).
    assert auth_oidc._normalize_allow_values([""]) == []
    assert auth_oidc._normalize_allow_values(["Hermes Users", "", "  "]) == ["Hermes Users"]

    # Scopes: space-delimited per RFC 6749 §3.3 — whitespace splitting is retained.
    assert auth_oidc._normalize_text_list("openid profile email") == ["openid", "profile", "email"]
    scopes = auth_oidc._normalize_scopes("openid profile email")
    assert scopes[:3] == ["openid", "profile", "email"]
