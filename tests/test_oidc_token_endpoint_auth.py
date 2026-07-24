"""Token-endpoint client authentication method selection for native OIDC.

RFC 6749 section 2.3.1 only requires authorization servers to support HTTP
Basic (`client_secret_basic`); `client_secret_post` is optional. Providers
built on strict OIDC stacks (e.g. zitadel/oidc) advertise
`token_endpoint_auth_methods_supported: ["none", "client_secret_basic"]` and
reject secrets sent in the form body. These tests pin the selection logic:
keep the historical form-body behavior whenever the provider accepts it (or
does not say), and switch to the Basic header only when the discovery document
announces that `client_secret_post` is unavailable — or when the operator
explicitly configures a method.
"""

import base64
import time
from urllib.parse import unquote


def _base_cfg(**overrides):
    cfg = {
        "issuer": "https://issuer.example",
        "client_id": "webui-client",
        "client_secret": "s3cret",
        "redirect_uri": "",
        "scopes": ["openid"],
        "allow_claim": "email",
        "allow_values": ["user@example.com"],
        "token_auth_method": "auto",
    }
    cfg.update(overrides)
    return cfg


def _run_token_exchange(monkeypatch, *, cfg, discovery_extra=None):
    """Drive complete_authorization_code_flow far enough to capture the token
    request, returning (form_data, headers) as seen by _post_form_json."""
    import api.auth_oidc as auth_oidc

    discovery = {
        "issuer": "https://issuer.example",
        "token_endpoint": "https://issuer.example/token",
        "jwks_uri": "https://issuer.example/jwks",
    }
    discovery.update(discovery_extra or {})

    captured = {}

    def fake_post(url, form_data, headers=None):
        captured["url"] = url
        captured["form"] = dict(form_data)
        captured["headers"] = dict(headers or {})
        return {"id_token": "stub-id-token"}

    monkeypatch.setattr(auth_oidc, "_resolve_oidc_config", lambda: cfg)
    monkeypatch.setattr(auth_oidc, "_get_discovery_document", lambda _issuer: discovery)
    monkeypatch.setattr(auth_oidc, "_post_form_json", fake_post)
    monkeypatch.setattr(
        auth_oidc,
        "_validate_id_token",
        lambda *_args, **_kwargs: {"sub": "user-1", "email": "user@example.com"},
    )
    auth_oidc._pending_flows.clear()
    auth_oidc._pending_flows["state-token"] = {
        "created_at": time.time(),
        "nonce": "nonce-token",
        "code_verifier": "verifier",
        "next_path": "/",
    }

    auth_oidc.complete_authorization_code_flow(
        "http://localhost:8787",
        "state-token",
        "code-token",
    )
    return captured["form"], captured["headers"]


def _decode_basic(headers):
    value = headers["Authorization"]
    assert value.startswith("Basic ")
    username, _, password = (
        base64.b64decode(value.removeprefix("Basic ")).decode("ascii").partition(":")
    )
    return unquote(username), unquote(password)


def test_auto_switches_to_basic_when_post_is_not_supported(monkeypatch):
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(),
        discovery_extra={
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        },
    )
    assert _decode_basic(headers) == ("webui-client", "s3cret")
    assert "client_secret" not in form
    # RFC 6749 2.3: use a single client authentication mechanism per request.
    assert "client_id" not in form
    assert form["code_verifier"] == "verifier"


def test_auto_keeps_post_when_provider_supports_it(monkeypatch):
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(),
        discovery_extra={
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
        },
    )
    assert "Authorization" not in headers
    assert form["client_secret"] == "s3cret"
    assert form["client_id"] == "webui-client"


def test_auto_keeps_post_when_discovery_omits_methods(monkeypatch):
    form, headers = _run_token_exchange(monkeypatch, cfg=_base_cfg())
    assert "Authorization" not in headers
    assert form["client_secret"] == "s3cret"


def test_configured_basic_wins_over_discovery(monkeypatch):
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(token_auth_method="client_secret_basic"),
        discovery_extra={
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
        },
    )
    assert _decode_basic(headers) == ("webui-client", "s3cret")
    assert "client_secret" not in form


def test_configured_post_wins_over_discovery(monkeypatch):
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(token_auth_method="client_secret_post"),
        discovery_extra={
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        },
    )
    assert "Authorization" not in headers
    assert form["client_secret"] == "s3cret"


def test_public_client_sends_no_credentials_either_way(monkeypatch):
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(client_secret=""),
        discovery_extra={
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        },
    )
    assert "Authorization" not in headers
    assert "client_secret" not in form
    assert form["client_id"] == "webui-client"


def test_basic_credentials_are_form_urlencoded_before_base64(monkeypatch):
    # RFC 6749 2.3.1: id/secret are application/x-www-form-urlencoded inside
    # the Basic credentials, so reserved characters must round-trip.
    form, headers = _run_token_exchange(
        monkeypatch,
        cfg=_base_cfg(client_id="client:acme%co", client_secret="p@ss:w%rd"),
        discovery_extra={
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        },
    )
    raw = headers["Authorization"].removeprefix("Basic ")
    decoded = base64.b64decode(raw).decode("ascii")
    encoded_user, _, encoded_pass = decoded.partition(":")
    assert unquote(encoded_user) == "client:acme%co"
    assert unquote(encoded_pass) == "p@ss:w%rd"
    # The encoded halves themselves must not contain a bare ":" so the
    # username/password split stays unambiguous.
    assert ":" not in encoded_user
    assert ":" not in encoded_pass
    assert "client_secret" not in form


def test_unknown_configured_method_falls_back_to_auto(monkeypatch):
    import api.auth_oidc as auth_oidc

    assert auth_oidc._normalize_token_auth_method("bogus") == "auto"
    assert auth_oidc._normalize_token_auth_method(None) == "auto"
    assert auth_oidc._normalize_token_auth_method("Client_Secret_Basic") == (
        "client_secret_basic"
    )
