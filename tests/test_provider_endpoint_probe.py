import json

from api import provider_endpoint_probe as probe


class _Response:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.payload


class _Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append((request, timeout))
        return self.response


def test_embedded_credentials_fail_before_transport():
    opener = _Opener(_Response(b'{"data":[]}'))
    result = probe.probe_models_endpoint(
        "custom",
        "https://user:password@example.invalid/v1",
        opener=opener,
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_url"
    assert opener.requests == []
    assert "password" not in json.dumps(result)


def test_response_body_is_bounded_and_rejected_when_oversized():
    response = _Response(b"x" * (probe.MAX_RESPONSE_BYTES + 1))
    result = probe.probe_models_endpoint(
        "custom",
        "https://models.example.invalid/v1",
        opener=_Opener(response),
    )
    assert response.read_sizes == [probe.MAX_RESPONSE_BYTES + 1]
    assert result == {
        "ok": False,
        "error": "parse",
        "detail": "response exceeded the 256 KB safety limit",
    }


def test_configured_private_endpoint_has_no_separate_dns_check_use_gap(monkeypatch):
    def unexpected_dns_preflight(*_args, **_kwargs):
        raise AssertionError("probe must not resolve once for policy and again to connect")

    monkeypatch.setattr(probe.socket, "getaddrinfo", unexpected_dns_preflight)
    response = _Response(b'{"data":[{"id":"local-model"}]}')
    result = probe.probe_models_endpoint(
        "custom",
        "http://model-server.internal:8080/v1",
        api_key="local-secret",
        opener=_Opener(response),
    )
    assert result == {
        "ok": True,
        "models": [{"id": "local-model", "label": "local-model"}],
    }


def test_probe_uses_inference_compatible_product_user_agent():
    response = _Response(b'{"data":[{"id":"remote-model"}]}')
    opener = _Opener(response)

    result = probe.probe_models_endpoint(
        "custom",
        "https://models.example.invalid/v1",
        opener=opener,
    )

    request, _timeout = opener.requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["user-agent"] == "Hermes-WebUI/1.0"
    assert result["ok"] is True
