"""OIDC SSRF guard: opt-in allowlist for issuer hosts on private addresses.

The guard in `_validate_outbound_oidc_url` refuses discovery/JWKS/token URLs
whose host resolves to a private, loopback or link-local address. An identity
provider on a split-horizon or VPN-only domain always resolves that way, so
`webui_oidc.allow_private_hosts` / `HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS`
lets the operator name exact hosts that may. Everything else stays guarded.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import api.auth_oidc as auth_oidc
from api.auth_oidc import OIDCAuthError, _fetch_json, _validate_outbound_oidc_url

IDP = "idp.home.arpa"
PRIVATE_IP = "10.0.0.5"
DISCOVERY = f"https://{IDP}/.well-known/openid-configuration"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.setattr(auth_oidc, "get_config", lambda: {})
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", (PRIVATE_IP, port))],
    )


def _config(monkeypatch, **webui_oidc):
    monkeypatch.setattr(auth_oidc, "get_config", lambda: {"webui_oidc": webui_oidc})


def _guarded(url=DISCOVERY):
    try:
        _validate_outbound_oidc_url(url)
    except OIDCAuthError:
        return True
    return False


# Default: unchanged behaviour.

def test_private_host_blocked_without_allowlist():
    assert _guarded()


def test_private_literal_ip_blocked_without_allowlist():
    assert _guarded(f"https://{PRIVATE_IP}/token")


def test_public_host_still_allowed(monkeypatch):
    monkeypatch.setattr(
        auth_oidc.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    assert not _guarded("https://idp.example.com/.well-known/openid-configuration")


# Opt-in.

def test_env_allowlist_admits_listed_host(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert not _guarded()


def test_config_allowlist_admits_listed_host(monkeypatch):
    _config(monkeypatch, allow_private_hosts=[IDP])
    assert not _guarded()


def test_env_overrides_config(monkeypatch):
    _config(monkeypatch, allow_private_hosts=[IDP])
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", "")
    assert _guarded()


@pytest.mark.parametrize(
    "listed",
    ["IdP.Home.ARPA", f"{IDP}.", f"other.lan, {IDP}", f"other.lan {IDP}", f"other.lan\n{IDP}"],
)
def test_allowlist_parsing_and_normalisation(monkeypatch, listed):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", listed)
    assert not _guarded()


def test_trailing_dot_in_url_matches_allowlist(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert not _guarded(f"https://{IDP}./.well-known/openid-configuration")


# Scope stays exact.

@pytest.mark.parametrize("other", ["evil.home.arpa", f"sub.{IDP}", f"{IDP}.evil.example"])
def test_unlisted_host_still_blocked(monkeypatch, other):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert _guarded(f"https://{other}/token")


def test_https_still_required_for_listed_host(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert _guarded(f"http://{IDP}/token")


def test_credentials_in_url_still_rejected_for_listed_host(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert _guarded(f"https://user:pass@{IDP}/token")


# Observable behaviour on the real fetch path: does a request leave the process?

class _RecordingOpener:
    def __init__(self):
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return io.BytesIO(json.dumps({"issuer": f"https://{IDP}"}).encode())


def test_fetch_sends_nothing_without_allowlist(monkeypatch):
    opener = _RecordingOpener()
    monkeypatch.setattr(auth_oidc, "_oidc_opener", lambda: opener)
    with pytest.raises(OIDCAuthError):
        _fetch_json(DISCOVERY)
    assert opener.urls == []


def test_fetch_reaches_listed_host(monkeypatch):
    opener = _RecordingOpener()
    monkeypatch.setattr(auth_oidc, "_oidc_opener", lambda: opener)
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_PRIVATE_HOSTS", IDP)
    assert _fetch_json(DISCOVERY) == {"issuer": f"https://{IDP}"}
    assert opener.urls == [DISCOVERY]
