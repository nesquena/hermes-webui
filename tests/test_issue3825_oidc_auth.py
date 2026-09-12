import io
import json
import socket
import ssl
import time
import urllib.error
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
        lambda _policy, _jwks_uri, **_kwargs: {
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
            policy=auth_oidc._OIDCNetworkPolicy(
                issuer="https://issuer.example", allow_private_endpoints=False
            ),
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
        lambda _policy, _jwks_uri, **_kwargs: {"keys": [_ec_jwk(private_key)]},
    )

    claims = auth_oidc._validate_id_token(
        token,
        client_id="webui-client",
        issuer="https://issuer.example",
        nonce="nonce-token",
        jwks_uri="https://issuer.example/jwks",
        policy=auth_oidc._OIDCNetworkPolicy(
            issuer="https://issuer.example", allow_private_endpoints=False
        ),
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
        lambda _policy, _issuer: {
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

    def fake_fetch_json(url, **_kwargs):
        fetches.append(url)
        return {"keys": [_ec_jwk(new_key, kid="new-key")]}

    monkeypatch.setattr(auth_oidc, "_fetch_json", fake_fetch_json)

    claims = auth_oidc._validate_id_token(
        token,
        client_id="webui-client",
        issuer="https://issuer.example",
        nonce="nonce-token",
        jwks_uri=jwks_uri,
        policy=auth_oidc._OIDCNetworkPolicy(
            issuer="https://issuer.example", allow_private_endpoints=False
        ),
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

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match=message):
        auth_oidc._fetch_json(url, policy=policy)


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
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )

    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration",
            policy=policy,
        )


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


# ---------------------------------------------------------------------------
# #6136 — OIDC SSRF protection: issuer-scoped opt-in for self-hosted providers
# ---------------------------------------------------------------------------


def test_allow_private_endpoints_matching_issuer_host(monkeypatch):
    """When allow_private_endpoints=True and the URL's origin (host AND port)
    matches the configured issuer's exact origin, private IP resolution must
    NOT raise."""
    import api.auth_oidc as auth_oidc

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://auth.internal.example", allow_private_endpoints=True
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ],
    )
    # URL on the exact configured origin — must skip the SSRF address check
    auth_oidc._validate_outbound_oidc_url(
        "https://auth.internal.example/.well-known/openid-configuration",
        policy=policy,
    )


def test_allow_private_grant_is_exact_origin_scoped_same_host_different_port(monkeypatch):
    """The allow-private grant is (host, port) scoped: a discovery-controlled
    endpoint on the SAME host but a DIFFERENT port must NOT inherit it."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://auth.internal.example:8443", allow_private_endpoints=True
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 9999))
        ],
    )
    # Same host, different port → NOT the exact origin → private blocked
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://auth.internal.example:9999/token", policy=policy
        )
    # Same host AND port (the exact origin) → grant applies
    auth_oidc._validate_outbound_oidc_url(
        "https://auth.internal.example:8443/.well-known/openid-configuration",
        policy=policy,
    )


def test_allow_private_grant_normalizes_effective_port(monkeypatch):
    """Effective-port normalization: an issuer without an explicit port is
    origin 443, so an explicit :443 URL is still the exact origin, while
    :8443 is not."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://auth.internal.example", allow_private_endpoints=True
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ],
    )
    # Explicit :443 equals the default 443 origin → allowed
    auth_oidc._validate_outbound_oidc_url(
        "https://auth.internal.example:443/.well-known/openid-configuration",
        policy=policy,
    )
    # Different port → blocked even though the host matches
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://auth.internal.example:8443/.well-known/openid-configuration",
            policy=policy,
        )


def test_allow_private_endpoints_different_host_still_blocked(monkeypatch):
    """When allow_private_endpoints=True but the URL host differs from the
    issuer host, private IPs must still be blocked (prevents discovery pivot)."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://auth.internal.example", allow_private_endpoints=True
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443))
        ],
    )
    # Different host from issuer — must still reject private IPs
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://evil-token.internal.example/token", policy=policy
        )


def test_allow_private_endpoints_default_false_blocks_private_dns(monkeypatch):
    """Without allow_private_endpoints=True, private IPs must still be blocked
    even when the URL is on the exact configured origin."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://auth.internal.example", allow_private_endpoints=False
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443))
        ],
    )
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://auth.internal.example/.well-known/openid-configuration",
            policy=policy,
        )


class _FakeOpener:
    def __init__(self, payload=None, capture=None, error=None):
        self.payload = payload
        self.capture = capture
        self.error = error

    def open(self, req, timeout=None):
        if self.capture is not None:
            self.capture["req"] = req
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "address",
    [
        # IPv4: loopback, private (RFC1918), link-local/metadata, multicast,
        # unspecified, reserved
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.7",
        "169.254.169.254",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "240.0.0.1",
        # Shared address space (100.64.0.0/10): neither private nor global,
        # must still be rejected (CGNAT carrier-grade NAT range)
        "100.64.0.1",
        "100.127.255.254",
        # IPv6: loopback, ULA, link-local, multicast, unspecified
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::",
        # IPv4-mapped IPv6 forms of loopback/private/link-local/shared
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:100.64.0.1",
    ],
)
def test_validate_outbound_url_rejects_special_address_ranges(address):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            f"https://{host}/.well-known/openid-configuration", policy=policy
        )


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8"])
def test_validate_outbound_url_allows_public_address_ranges(address):
    import api.auth_oidc as auth_oidc

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    host = f"[{address}]" if ":" in address else address
    auth_oidc._validate_outbound_oidc_url(
        f"https://{host}/.well-known/openid-configuration", policy=policy
    )


def test_exact_origin_loopback_allowance_is_explicit_and_scoped(monkeypatch):
    """Loopback is reachable ONLY for the exact configured origin under the
    explicit opt-in; the same loopback on another port stays blocked, and
    without the opt-in the exact origin itself stays blocked."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8443))],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://127.0.0.1:8443", allow_private_endpoints=True
    )
    # Exact loopback origin under the opt-in → allowed
    auth_oidc._validate_outbound_oidc_url(
        "https://127.0.0.1:8443/.well-known/openid-configuration", policy=policy
    )
    # Same loopback host, different port → blocked
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://127.0.0.1:9999/token", policy=policy
        )
    # No opt-in → even the exact loopback origin is blocked
    policy_no_optin = auth_oidc._OIDCNetworkPolicy(
        issuer="https://127.0.0.1:8443", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._validate_outbound_oidc_url(
            "https://127.0.0.1:8443/.well-known/openid-configuration",
            policy=policy_no_optin,
        )


def test_exact_origin_never_reaches_link_local_metadata(monkeypatch):
    """Even with the opt-in, the exact configured origin must never reach
    link-local / cloud-metadata addresses (169.254.0.0/16)."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=True
    )
    # URL is the exact origin (URL-level check skipped), but the pinned
    # connection classifies link-local as always-disallowed → fail closed.
    with pytest.raises(OIDCAuthError, match="resolved only to disallowed addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_fetch_json_fails_closed_on_dns_error(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    def boom(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(auth_oidc.socket, "getaddrinfo", boom)
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="Failed to resolve OIDC endpoint host"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_fetch_json_fails_closed_on_empty_dns_answers(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(auth_oidc.socket, "getaddrinfo", lambda *a, **k: [])
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="resolved to no addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_fetch_json_rejects_mixed_public_private_answers(monkeypatch):
    """A non-exact-origin host that resolves to a mix of public and private
    addresses is rejected outright (fail closed on mixed answers)."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_fetch_json_fails_closed_on_rebinding_to_metadata(monkeypatch):
    """DNS rebinding / peer mismatch: the URL-level precheck passes (as if the
    first resolution were public), but the connect-time resolution flips to
    the cloud metadata address — the pinned connection must fail closed."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    # Precheck sees a clean resolution...
    monkeypatch.setattr(auth_oidc, "_is_disallowed_oidc_host", lambda _host: False)
    # ...then the actual connection resolves to link-local metadata.
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="resolved only to disallowed addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_pinned_connection_binds_to_first_approved_address_and_keeps_sni(monkeypatch):
    """The connection resolves ONCE, skips disallowed addresses, pins the
    socket to the first approved (public) address, and still verifies the TLS
    hostname (server_hostname) against the requested host."""
    import api.auth_oidc as auth_oidc

    captured = {}

    class FakeSocket:
        def __init__(self, family, socktype, proto):
            captured["family"] = family
            captured["socktype"] = socktype

        def settimeout(self, timeout):
            captured["timeout"] = timeout

        def connect(self, sockaddr):
            captured["sockaddr"] = sockaddr

        def close(self):
            pass

    def fake_wrap_socket(self, sock, *, server_hostname=None, **kwargs):
        captured["server_hostname"] = server_hostname
        return sock

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443)),
        ],
    )
    monkeypatch.setattr(auth_oidc.socket, "socket", FakeSocket)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    conn.connect()

    # Pinned to the first approved address of the SINGLE resolution — never
    # to the private one, never to a re-resolved address.
    assert captured["sockaddr"] == ("93.184.216.34", 443)
    # TLS hostname verification / SNI preserved against the requested host.
    assert captured["server_hostname"] == "issuer.example"
    assert conn.sock is not None


def test_pinned_connection_fails_closed_when_only_disallowed_addresses(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    with pytest.raises(OIDCAuthError, match="disallowed addresses"):
        conn.connect()


def test_pinned_connection_fails_closed_on_dns_error(monkeypatch):
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    def boom(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(auth_oidc.socket, "getaddrinfo", boom)
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    with pytest.raises(OIDCAuthError, match="Failed to resolve OIDC endpoint host"):
        conn.connect()


def test_pinned_connection_tries_next_approved_address_on_connect_failure(monkeypatch):
    """If the first approved address cannot connect, later approved answers
    from the SAME resolution are tried in resolver order; rejected addresses
    are never attempted; failed sockets are closed."""
    import api.auth_oidc as auth_oidc

    attempted = []
    closed = []

    class FakeSocket:
        def __init__(self, family, socktype, proto):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, sockaddr):
            attempted.append(sockaddr)
            if sockaddr[0] == "93.184.216.34":
                raise OSError("connection refused")

        def close(self):
            closed.append(True)

    def fake_wrap_socket(self, sock, *, server_hostname=None, **kwargs):
        return sock

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ],
    )
    monkeypatch.setattr(auth_oidc.socket, "socket", FakeSocket)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    conn.connect()

    # First approved address failed to connect; the second approved address
    # was tried and won.  The private (rejected) address was never attempted.
    assert attempted == [("93.184.216.34", 443), ("8.8.8.8", 443)]
    # Only the failed socket was closed; the successful one stays open.
    assert len(closed) == 1
    assert conn.sock is not None


def test_pinned_connection_closes_socket_when_tls_wrap_fails(monkeypatch):
    """If TLS wrapping fails, the raw socket is closed before the error
    propagates — no descriptor leak."""
    import api.auth_oidc as auth_oidc

    closed = []

    class FakeSocket:
        def __init__(self, family, socktype, proto):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, sockaddr):
            pass

        def close(self):
            closed.append(True)

    def boom_wrap_socket(self, sock, *, server_hostname=None, **kwargs):
        raise ssl.SSLError("TLS handshake failed")

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(auth_oidc.socket, "socket", FakeSocket)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", boom_wrap_socket)

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    with pytest.raises(ssl.SSLError):
        conn.connect()
    assert len(closed) == 1


def test_pinned_connection_fails_closed_on_proxy_tunnel(monkeypatch):
    """A proxy CONNECT tunnel is refused outright: urllib sets _tunnel_host
    when routing through a proxy, and the pinned connection must never
    change its connection authority."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    conn = auth_oidc._PinnedHTTPSConnection("issuer.example", 443, policy=policy, timeout=10)
    conn._tunnel_host = "proxy.example"
    with pytest.raises(OIDCAuthError, match="tunnel"):
        conn.connect()


def test_oidc_opener_disables_ambient_proxies(monkeypatch):
    """Ambient HTTP(S)_PROXY settings must never redirect the OIDC flow: the
    opener installs an explicit EMPTY ProxyHandler, so urllib cannot change
    the connection authority to a proxy."""
    import api.auth_oidc as auth_oidc
    import urllib.request as urllib_request

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:3128")

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    opener = auth_oidc._oidc_opener(policy)
    # The ambient proxy configuration above must be visible to the process
    # (otherwise this test would be vacuous).
    assert urllib_request.getproxies(), "ambient proxy env must be configured"
    proxy_handlers = [
        h for h in opener.handlers if isinstance(h, urllib_request.ProxyHandler)
    ]
    # CPython >= 3.11 registers a ProxyHandler only when it exposes protocol
    # methods, so the empty ProxyHandler({}) the opener installs never appears
    # in the handler chain. The guarantee is that no handler carries the
    # ambient proxy map (an absent or empty handler cannot redirect the flow);
    # the functional no-proxy property is enforced end-to-end by
    # test_token_form_body_never_delivered_to_ambient_proxy, and a tunnel
    # attempt fails closed via test_pinned_connection_fails_closed_on_proxy_tunnel.
    assert not any(h.proxies for h in proxy_handlers)
    for handler in proxy_handlers:
        assert handler.proxies == {}


def test_token_form_body_never_delivered_to_ambient_proxy(monkeypatch):
    """End-to-end guard: with an ambient HTTPS proxy configured, the token
    form body still travels ONLY to the pinned endpoint socket — a proxy
    that would receive the client_secret never sees a connection."""
    import api.auth_oidc as auth_oidc

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")

    connected_to = []

    class FakeSocket:
        def __init__(self, family, socktype, proto):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, sockaddr):
            connected_to.append(sockaddr)

        def sendall(self, data):
            pass

        def makefile(self, *args, **kwargs):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
            )

        def close(self):
            pass

    def fake_wrap_socket(self, sock, *, server_hostname=None, **kwargs):
        return sock

    monkeypatch.setattr(auth_oidc.socket, "socket", FakeSocket)
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    auth_oidc._post_form_json(
        "https://issuer.example/token",
        {
            "grant_type": "authorization_code",
            "client_id": "webui-client",
            "code": "auth-code",
            "client_secret": "super-secret",
        },
        policy=policy,
    )

    # The connection went straight to the endpoint — never to the ambient
    # proxy (127.0.0.1:3128).  The proxy never had a chance to read the body.
    assert connected_to == [("93.184.216.34", 443)]


def test_fetch_json_rejects_dns_resolved_shared_space(monkeypatch):
    """A discovery-controlled hostname resolving into shared address space
    (100.64.0.0/10, CGNAT) is rejected even though CPython classifies it as
    neither private nor global."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))
        ],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration",
            policy=policy,
        )


def test_fetch_json_rejects_foreign_origin_resolving_to_shared_space(monkeypatch):
    """Even with the private opt-in enabled, a FOREIGN origin resolving to
    shared address space stays blocked — the grant is exact-origin scoped."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))
        ],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=True
    )
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc._fetch_json(
            "https://evil.example/.well-known/openid-configuration",
            policy=policy,
        )


def test_exact_origin_shared_space_allowed_under_optin(monkeypatch):
    """The self-hosted use case survives: with the explicit opt-in, the EXACT
    configured origin in shared address space is still reachable."""
    import api.auth_oidc as auth_oidc

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))
        ],
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://100.64.0.1", allow_private_endpoints=True
    )
    auth_oidc._validate_outbound_oidc_url(
        "https://100.64.0.1/.well-known/openid-configuration", policy=policy
    )


def test_address_allowed_rejects_shared_space_without_exact_origin_optin():
    """Both classifiers must treat shared address space as restricted: the
    connect-time policy only grants it to the EXACT configured origin under
    the explicit opt-in."""
    import ipaddress

    import api.auth_oidc as auth_oidc

    shared = ipaddress.ip_address("100.64.0.1")
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    assert policy.address_allowed("issuer.example", 443, shared) is False
    assert policy.address_allowed("100.64.0.1", 443, shared) is False

    optin = auth_oidc._OIDCNetworkPolicy(
        issuer="https://100.64.0.1", allow_private_endpoints=True
    )
    # Exact configured origin under the opt-in → granted.
    assert optin.address_allowed("100.64.0.1", 443, shared) is True
    # Same address, different host → still denied.
    assert optin.address_allowed("issuer.example", 443, shared) is False


def test_no_redirect_handler_refuses_all_redirects():
    """Redirects stay disabled: every 3xx hop is refused, never followed."""
    import api.auth_oidc as auth_oidc

    handler = auth_oidc._NoRedirect()
    assert (
        handler.redirect_request(None, None, 302, None, None, "https://evil.example/steal")
        is None
    )


def test_fetch_json_surfaces_redirect_as_failure(monkeypatch):
    """A 302 response is not followed; it surfaces as an OIDCAuthError."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        auth_oidc,
        "_oidc_opener",
        lambda _policy: _FakeOpener(
            error=urllib.error.HTTPError(
                "https://issuer.example/", 302, "Found", {}, None
            )
        ),
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    with pytest.raises(OIDCAuthError, match="Failed to reach OIDC endpoint"):
        auth_oidc._fetch_json(
            "https://issuer.example/.well-known/openid-configuration", policy=policy
        )


def test_fetch_json_sends_get_with_accept_header(monkeypatch):
    import api.auth_oidc as auth_oidc

    captured = {}
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        auth_oidc, "_oidc_opener", lambda _policy: _FakeOpener({"ok": True}, captured)
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    result = auth_oidc._fetch_json(
        "https://issuer.example/.well-known/openid-configuration", policy=policy
    )

    req = captured["req"]
    assert req.get_method() == "GET"
    assert req.full_url == "https://issuer.example/.well-known/openid-configuration"
    assert any(
        k.lower() == "accept" and v == "application/json"
        for k, v in req.header_items()
    )
    assert result == {"ok": True}


def test_post_form_json_sends_exact_method_target_and_secrets(monkeypatch):
    """The token exchange is a POST to the exact discovery-controlled target;
    the client_secret travels in the form body — never in an Authorization
    header."""
    import api.auth_oidc as auth_oidc

    captured = {}
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        auth_oidc,
        "_oidc_opener",
        lambda _policy: _FakeOpener({"access_token": "at"}, captured),
    )
    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example", allow_private_endpoints=False
    )
    result = auth_oidc._post_form_json(
        "https://issuer.example/token",
        {
            "grant_type": "authorization_code",
            "client_id": "webui-client",
            "code": "auth-code",
            "code_verifier": "pkce-verifier",
            "client_secret": "super-secret",
        },
        policy=policy,
    )

    req = captured["req"]
    assert req.get_method() == "POST"
    assert req.full_url == "https://issuer.example/token"
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    body = req.data.decode("utf-8")
    assert "client_secret=super-secret" in body
    assert "code_verifier=pkce-verifier" in body
    assert "authorization" not in headers
    assert result == {"access_token": "at"}


def test_oidc_network_policy_is_immutable():
    """The policy is parsed once and cannot be mutated afterwards."""
    import api.auth_oidc as auth_oidc

    policy = auth_oidc._OIDCNetworkPolicy(
        issuer="https://issuer.example:8443", allow_private_endpoints=True
    )
    assert (policy.origin_host, policy.origin_port) == ("issuer.example", 8443)
    assert policy.allow_private_endpoints is True
    with pytest.raises(AttributeError):
        policy.origin_host = "evil.example"
    with pytest.raises(AttributeError):
        policy.allow_private_endpoints = False
    # Non-https issuers are rejected at policy construction time.
    with pytest.raises(auth_oidc.OIDCConfigError):
        auth_oidc._OIDCNetworkPolicy(
            issuer="http://issuer.example", allow_private_endpoints=True
        )


def test_start_flow_requires_present_discovery_issuer(monkeypatch):
    """The start flow must NOT consume authorization_endpoint unless the
    discovery document declares a present issuer."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc,
        "_require_oidc_config",
        lambda: {
            "issuer": "https://issuer.example",
            "client_id": "webui-client",
            "client_secret": "",
            "redirect_uri": "",
            "scopes": ["openid"],
            "allow_claim": "email",
            "allow_values": ["user@example.com"],
            "allow_private_endpoints": False,
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _policy, _issuer: {
            "authorization_endpoint": "https://issuer.example/authorize",
            "token_endpoint": "https://issuer.example/token",
            "jwks_uri": "https://issuer.example/jwks",
            # "issuer" deliberately missing
        },
    )
    with pytest.raises(OIDCAuthError, match="did not include an issuer"):
        auth_oidc.build_authorization_redirect("http://localhost:8787")


def test_start_flow_rejects_mismatched_discovery_issuer(monkeypatch):
    """The start flow must reject a discovery document whose issuer differs
    from the configured issuer."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc,
        "_require_oidc_config",
        lambda: {
            "issuer": "https://issuer.example",
            "client_id": "webui-client",
            "client_secret": "",
            "redirect_uri": "",
            "scopes": ["openid"],
            "allow_claim": "email",
            "allow_values": ["user@example.com"],
            "allow_private_endpoints": False,
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _policy, _issuer: {
            "issuer": "https://evil.example",
            "authorization_endpoint": "https://issuer.example/authorize",
            "token_endpoint": "https://issuer.example/token",
            "jwks_uri": "https://issuer.example/jwks",
        },
    )
    with pytest.raises(OIDCAuthError, match="did not match the configured issuer"):
        auth_oidc.build_authorization_redirect("http://localhost:8787")


def test_callback_flow_requires_present_discovery_issuer(monkeypatch):
    """The callback flow must reject a discovery document with NO issuer —
    a missing issuer is as fatal as a mismatch."""
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
            "allow_private_endpoints": False,
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _policy, _issuer: {
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
    with pytest.raises(OIDCAuthError, match="did not include an issuer"):
        auth_oidc.complete_authorization_code_flow(
            "http://localhost:8787", "state-token", "code-token"
        )


def test_callback_flow_blocks_same_host_different_port_token_pivot(monkeypatch):
    """#6136 re-gate: a discovery-controlled token_endpoint on the SAME host
    but a DIFFERENT port must NOT inherit the allow-private grant."""
    import api.auth_oidc as auth_oidc
    from api.auth_oidc import OIDCAuthError

    monkeypatch.setattr(
        auth_oidc,
        "_resolve_oidc_config",
        lambda: {
            "issuer": "https://auth.internal.example:8443",
            "client_id": "webui-client",
            "client_secret": "",
            "redirect_uri": "",
            "scopes": ["openid"],
            "allow_claim": "email",
            "allow_values": ["user@example.com"],
            "allow_private_endpoints": True,
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _policy, _issuer: {
            "issuer": "https://auth.internal.example:8443",
            "token_endpoint": "https://auth.internal.example:9999/token",
            "jwks_uri": "https://auth.internal.example:8443/jwks",
        },
    )
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.7", 9999))],
    )
    auth_oidc._pending_flows.clear()
    auth_oidc._pending_flows["state-token"] = {
        "created_at": time.time(),
        "nonce": "nonce-token",
        "code_verifier": "verifier",
        "next_path": "/",
    }
    with pytest.raises(OIDCAuthError, match="private or local addresses"):
        auth_oidc.complete_authorization_code_flow(
            "http://localhost:8787", "state-token", "code-token"
        )


def test_callback_flow_allows_exact_origin_private_endpoints_end_to_end(monkeypatch):
    """The full callback flow (discovery → token → id_token) succeeds when the
    discovery-controlled endpoints sit on the exact configured origin and the
    opt-in is enabled — the immutable policy is carried through every hop."""
    import api.auth_oidc as auth_oidc

    monkeypatch.setattr(
        auth_oidc,
        "_resolve_oidc_config",
        lambda: {
            "issuer": "https://auth.internal.example:8443",
            "client_id": "webui-client",
            "client_secret": "client-secret",
            "redirect_uri": "",
            "scopes": ["openid"],
            "allow_claim": "email",
            "allow_values": ["user@example.com"],
            "allow_private_endpoints": True,
        },
    )
    monkeypatch.setattr(
        auth_oidc,
        "_get_discovery_document",
        lambda _policy, _issuer: {
            "issuer": "https://auth.internal.example:8443",
            "token_endpoint": "https://auth.internal.example:8443/token",
            "jwks_uri": "https://auth.internal.example:8443/jwks",
        },
    )
    # The exact origin resolves privately — permitted by the scoped grant.
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 8443))],
    )
    captured = {}
    monkeypatch.setattr(
        auth_oidc,
        "_oidc_opener",
        lambda _policy: _FakeOpener({"id_token": "abc.def.ghi"}, captured),
    )
    monkeypatch.setattr(
        auth_oidc,
        "_validate_id_token",
        lambda _token, **_kwargs: {"sub": "user-123", "email": "user@example.com"},
    )
    auth_oidc._pending_flows.clear()
    auth_oidc._pending_flows["state-token"] = {
        "created_at": time.time(),
        "nonce": "nonce-token",
        "code_verifier": "verifier",
        "next_path": "/chat/1",
    }

    result = auth_oidc.complete_authorization_code_flow(
        "http://localhost:8787", "state-token", "code-token"
    )

    assert result["subject"] == "user-123"
    assert result["email"] == "user@example.com"
    assert result["next_path"] == "/chat/1"
    # The token exchange hit the exact-origin token endpoint.
    assert captured["req"].full_url == "https://auth.internal.example:8443/token"
