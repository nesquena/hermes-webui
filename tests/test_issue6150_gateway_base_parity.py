"""Parity of the gateway-base resolver in ``api/updates.py`` with the canonical one (#6150, #6289).

The live Agent-version probe appends ``/health`` and ``/health/detailed`` to the
base it resolves, so the resolver has to behave exactly like
``api.agent_health._remote_gateway_base_url`` -- which documents itself as
mirroring ``api/updates.py``. Two shapes diverged:

* ``api/updates.py`` applied ``.strip()`` only after the ``or`` chain, so a
  whitespace-only high-priority variable was truthy before stripping and
  shadowed every valid lower-priority URL (and the Docker default), leaving an
  empty base and no probe at all;
* the health-path suffix normalization only knew ``/health/detailed`` and
  ``/health``, so a gateway configured as ``.../v1/health`` or ``.../status``
  was probed at ``/v1/health/health`` and never answered.

The last test drives the real probe against a local HTTP server, so the
requirement is measured through the production path rather than on the helper
alone.
"""
import http.server
import json
import threading

import pytest

from api.agent_health import _remote_gateway_base_url
from api.updates import _detect_agent_version_from_gateway_health, _gateway_health_base_url

SUPPORTED_VARS = (
    'GATEWAY_HEALTH_URL',
    'HERMES_GATEWAY_HEALTH_URL',
    'HERMES_API_URL',
    'HERMES_WEBUI_GATEWAY_BASE_URL',
)
DOCKER_DEFAULT = 'http://hermes-agent:8642'


@pytest.fixture(autouse=True)
def _clean_gateway_env(monkeypatch):
    for var in SUPPORTED_VARS:
        monkeypatch.delenv(var, raising=False)


def _set_all(monkeypatch, value: str) -> None:
    for var in SUPPORTED_VARS:
        monkeypatch.setenv(var, value)


@pytest.mark.parametrize('var', SUPPORTED_VARS)
def test_each_supported_variable_resolves_alone(var, monkeypatch):
    monkeypatch.setenv(var, 'http://gw.example:9864')
    assert _gateway_health_base_url() == 'http://gw.example:9864'


def test_precedence_follows_the_documented_order(monkeypatch):
    for idx, var in enumerate(SUPPORTED_VARS):
        for lower in SUPPORTED_VARS[idx + 1:]:
            monkeypatch.setenv(var, 'http://high.example:1')
            monkeypatch.setenv(lower, 'http://low.example:2')
            assert _gateway_health_base_url() == 'http://high.example:1', (var, lower)
            monkeypatch.delenv(lower)


def test_no_variable_falls_back_to_the_docker_default():
    assert _gateway_health_base_url() == DOCKER_DEFAULT
    assert _remote_gateway_base_url() is None


@pytest.mark.parametrize('var', SUPPORTED_VARS[:-1])
def test_whitespace_only_high_priority_falls_through(var, monkeypatch):
    """A blank high-priority variable must not shadow a valid lower-priority URL."""
    lower = SUPPORTED_VARS[SUPPORTED_VARS.index(var) + 1]
    monkeypatch.setenv(var, '   ')
    monkeypatch.setenv(lower, 'http://lower.example:9864')
    assert _gateway_health_base_url() == 'http://lower.example:9864'


def test_all_whitespace_falls_back_to_the_docker_default(monkeypatch):
    _set_all(monkeypatch, '   ')
    assert _gateway_health_base_url() == DOCKER_DEFAULT


@pytest.mark.parametrize('var', SUPPORTED_VARS)
@pytest.mark.parametrize(
    'url,expected',
    [
        ('http://gw.example:9864/health', 'http://gw.example:9864'),
        ('http://gw.example:9864/health/detailed', 'http://gw.example:9864'),
        ('http://gw.example:9864/v1/health', 'http://gw.example:9864/v1'),
        ('http://gw.example:9864/status', 'http://gw.example:9864'),
        ('http://gw.example:9864/health/', 'http://gw.example:9864'),
    ],
)
def test_health_path_suffix_is_normalized(var, url, expected, monkeypatch):
    monkeypatch.setenv(var, url)
    assert _gateway_health_base_url() == expected


@pytest.mark.parametrize(
    'url',
    [
        'http://gw.example:9864',
        'http://gw.example:9864/v1',
        'http://gw.example:9864/api/proxy',
        'https://gw.example',
    ],
)
def test_non_health_path_is_preserved(url, monkeypatch):
    monkeypatch.setenv('HERMES_API_URL', url)
    assert _gateway_health_base_url() == url


@pytest.mark.parametrize(
    'env',
    [
        {},
        {'GATEWAY_HEALTH_URL': '   '},
        {'GATEWAY_HEALTH_URL': '   ', 'HERMES_API_URL': 'http://lower.example:9864'},
        {'HERMES_GATEWAY_HEALTH_URL': ' http://gw.example:9864/health '},
        {'HERMES_API_URL': 'http://gw.example:9864/v1/health/'},
        {'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://gw.example:9864/status'},
        {'GATEWAY_HEALTH_URL': '  ', 'HERMES_GATEWAY_HEALTH_URL': ' ',
         'HERMES_API_URL': '  ', 'HERMES_WEBUI_GATEWAY_BASE_URL': ' '},
    ],
)
def test_matches_the_canonical_resolver(env, monkeypatch):
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    canonical = _remote_gateway_base_url() or DOCKER_DEFAULT
    assert _gateway_health_base_url() == canonical, env


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    version = 'v9.9.9'

    def do_GET(self):  # noqa: N802 - http.server API
        payload = json.dumps({'status': 'ok', 'version': self.version}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # pragma: no cover - silence the test server
        pass


@pytest.fixture()
def local_gateway():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_reaches_the_gateway_behind_a_blank_high_priority_variable(local_gateway, monkeypatch):
    """The resolver defect has to be measured through the real probe, not the helper."""
    monkeypatch.setenv('GATEWAY_HEALTH_URL', '   ')
    monkeypatch.setenv('HERMES_API_URL', local_gateway)
    assert _detect_agent_version_from_gateway_health() == 'v9.9.9'
