from __future__ import annotations

import pytest

from api.gateway_authority import (
    REMOTE_GATEWAY_CONTROL_ERROR_CODE,
    RemoteGatewayControlUnsupported,
    remote_gateway_configured,
    require_local_gateway_control,
)


_REMOTE_ENV_VARS = ("HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL")
_HEALTH_ONLY_ENV_VARS = ("GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL")


def _clear_gateway_urls(monkeypatch) -> None:
    for name in (*_REMOTE_ENV_VARS, *_HEALTH_ONLY_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


def test_gateway_authority_is_local_without_remote_runtime_url(monkeypatch):
    _clear_gateway_urls(monkeypatch)

    assert remote_gateway_configured() is False
    require_local_gateway_control()


def test_each_runtime_url_selects_remote_gateway_authority(monkeypatch):
    _clear_gateway_urls(monkeypatch)

    for name in _REMOTE_ENV_VARS:
        monkeypatch.setenv(name, "http://hermes-agent:8642")
        assert remote_gateway_configured() is True
        with pytest.raises(RemoteGatewayControlUnsupported) as caught:
            require_local_gateway_control()
        assert caught.value.error_code == REMOTE_GATEWAY_CONTROL_ERROR_CODE
        monkeypatch.delenv(name)


def test_health_probe_url_does_not_grant_remote_lifecycle_authority(monkeypatch):
    _clear_gateway_urls(monkeypatch)

    for name in _HEALTH_ONLY_ENV_VARS:
        monkeypatch.setenv(name, "http://hermes-agent:8642/health")

    assert remote_gateway_configured() is False
    require_local_gateway_control()
