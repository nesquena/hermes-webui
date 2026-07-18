"""Tests for GET /api/mcp/servers/{name}/test — the MCP connection-test route.

Standalone mode (default under tests/conftest.py, which sets
HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE=1) exercises the plain HTTP
reachability fallback. Bridge-path tests activate a faked agent checkout
using the same sys.modules-faking pattern as tests/test_agent_config_bridge.py
so behavior is identical on CI (no checkout) and dev machines (real checkout).
"""
import json
import socket
import sys
import types
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from api.routes import _handle_mcp_server_test, _standalone_mcp_url_probe
from api import agent_config_bridge as bridge


def _make_handler():
    h = MagicMock()
    h.path = '/api/mcp/servers/test-server/test'
    h.command = 'GET'
    return h


def _json_payload(handler):
    body = handler.wfile.write.call_args[0][0]
    return json.loads(body.decode('utf-8'))


SAMPLE_SERVERS = {
    "web-srv": {"url": "http://localhost:4000/mcp", "timeout": 60},
    "stdio-srv": {"command": "mcp-searxng", "args": ["--port", "8888"]},
}


class TestNotFound:
    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_unknown_server_returns_404(self, mock_home, mock_cfg):
        mock_home.return_value = object()
        mock_cfg.return_value = {'mcp_servers': SAMPLE_SERVERS}
        h = _make_handler()
        _handle_mcp_server_test(h, 'missing')
        status = h.send_response.call_args[0][0]
        assert status == 404

    def test_empty_name_rejected(self):
        h = _make_handler()
        _handle_mcp_server_test(h, '')
        status = h.send_response.call_args[0][0]
        assert status == 400


class TestStandaloneFallback:
    """Bridge disabled (conftest default) — plain HTTP reachability check."""

    @patch('api.routes._standalone_mcp_url_probe')
    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_url_server_reachable(self, mock_home, mock_cfg, mock_probe):
        mock_home.return_value = object()
        mock_cfg.return_value = {'mcp_servers': SAMPLE_SERVERS}
        mock_probe.return_value = {"ok": True, "latency_ms": 12, "http_status": 200}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        mock_probe.assert_called_once_with('http://localhost:4000/mcp')
        payload = _json_payload(h)
        assert payload['ok'] is True
        assert payload['latency_ms'] == 12

    @patch('api.routes._standalone_mcp_url_probe')
    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_url_server_unreachable(self, mock_home, mock_cfg, mock_probe):
        mock_home.return_value = object()
        mock_cfg.return_value = {'mcp_servers': SAMPLE_SERVERS}
        mock_probe.return_value = {"ok": False, "error": "Connection refused"}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        payload = _json_payload(h)
        assert payload['ok'] is False
        assert 'Connection refused' in payload['error']

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_stdio_server_unsupported(self, mock_home, mock_cfg):
        mock_home.return_value = object()
        mock_cfg.return_value = {'mcp_servers': SAMPLE_SERVERS}
        h = _make_handler()
        _handle_mcp_server_test(h, 'stdio-srv')
        payload = _json_payload(h)
        assert payload['ok'] is False
        assert payload['supported'] is False
        assert 'agent checkout' in payload['reason']


class TestStandaloneUrlProbeNetworkSeam:
    """_standalone_mcp_url_probe itself, with the HTTP layer mocked out."""

    def test_reachable_head_ok(self):
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self: fake_resp
        fake_resp.__exit__ = lambda self, *a: False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp
        with patch('api.routes.build_opener', return_value=fake_opener):
            result = _standalone_mcp_url_probe('http://localhost:4000/mcp')
        assert result['ok'] is True
        assert result['http_status'] == 200
        assert 'latency_ms' in result

    def test_http_error_status_still_reachable(self):
        from urllib.error import HTTPError
        fake_opener = MagicMock()
        fake_opener.open.side_effect = HTTPError('http://x', 405, 'Method Not Allowed', {}, None)
        with patch('api.routes.build_opener', return_value=fake_opener):
            result = _standalone_mcp_url_probe('http://localhost:4000/mcp')
        assert result['ok'] is True
        assert result['http_status'] == 405

    def test_connection_failure_is_unreachable(self):
        fake_opener = MagicMock()
        fake_opener.open.side_effect = URLError(ConnectionRefusedError('refused'))
        with patch('api.routes.build_opener', return_value=fake_opener):
            result = _standalone_mcp_url_probe('http://localhost:4000/mcp')
        assert result['ok'] is False
        assert 'error' in result

    def test_timeout_is_unreachable(self):
        fake_opener = MagicMock()
        fake_opener.open.side_effect = socket.timeout('timed out')
        with patch('api.routes.build_opener', return_value=fake_opener):
            result = _standalone_mcp_url_probe('http://localhost:4000/mcp')
        assert result['ok'] is False


class _FakeProbeAgent:
    """Fakes just enough of hermes_cli.mcp_config for probe_mcp_server()."""

    def __init__(self, tools=None, raise_exc=None, oauth_needed=False, token_present=True):
        self._tools = tools if tools is not None else [("search", "desc")]
        self._raise_exc = raise_exc
        self.oauth_needed = oauth_needed
        self.token_present = token_present
        self.probed_with = None

        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.set_hermes_home_override = lambda path: object()
        hermes_constants.reset_hermes_home_override = lambda token: None

        mcp_mod = types.ModuleType("hermes_cli.mcp_config")

        def _get_mcp_servers():
            return {"web-srv": {"url": "http://x", "auth": "oauth" if oauth_needed else None}}

        def _probe_single_server(name, cfg, connect_timeout=None, *, details=None):
            self.probed_with = (name, connect_timeout)
            if self._raise_exc:
                raise self._raise_exc
            if details is not None:
                details["prompts"] = 2
                details["resources"] = 1
            return self._tools

        def _oauth_tokens_present(name):
            return self.token_present

        mcp_mod._get_mcp_servers = _get_mcp_servers
        mcp_mod._probe_single_server = _probe_single_server
        mcp_mod._oauth_tokens_present = _oauth_tokens_present

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []

        # _probe_import() validates the same config contract used by all
        # bridge operations before the route may call the probe.  Include it
        # explicitly so these tests cannot accidentally import a developer's
        # real hermes_cli.config module or silently exercise the 503 path.
        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = lambda: {}
        config_mod.save_config = lambda config, **kwargs: None
        config_mod.save_env_value = lambda key, value: None

        self.modules = {
            "hermes_constants": hermes_constants,
            "hermes_cli": hermes_cli,
            "hermes_cli.config": config_mod,
            "hermes_cli.mcp_config": mcp_mod,
        }


@pytest.fixture
def fake_probe_agent(monkeypatch, tmp_path):
    def _activate(**kwargs):
        fake = _FakeProbeAgent(**kwargs)
        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        for name, module in fake.modules.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        assert bridge.bridge_available() is True
        return fake
    yield _activate
    bridge._import_state = None


class TestBridgePath:
    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_probe_ok(self, mock_home, mock_cfg, fake_probe_agent, tmp_path):
        fake = fake_probe_agent(tools=[("a", "d"), ("b", "d")])
        mock_home.return_value = tmp_path
        mock_cfg.return_value = {'mcp_servers': {'web-srv': {'url': 'http://x'}}}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        payload = _json_payload(h)
        assert payload['ok'] is True
        assert payload['tools_count'] == 2
        assert payload['prompts'] == 2
        assert payload['resources'] == 1
        assert 'latency_ms' in payload
        # The route must not force a WebUI-side timeout override — see
        # TestProbeMcpServerBridgeFunction.test_default_timeout_does_not_override_server_connect_timeout.
        assert fake.probed_with == ('web-srv', None)

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_probe_connection_failure(self, mock_home, mock_cfg, fake_probe_agent, tmp_path):
        fake_probe_agent(raise_exc=RuntimeError("could not connect"))
        mock_home.return_value = tmp_path
        mock_cfg.return_value = {'mcp_servers': {'web-srv': {'url': 'http://x'}}}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        payload = _json_payload(h)
        assert payload['ok'] is False
        assert 'could not connect' in payload['error']

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_probe_oauth_missing_token(self, mock_home, mock_cfg, fake_probe_agent, tmp_path):
        fake_probe_agent(oauth_needed=True, token_present=False)
        mock_home.return_value = tmp_path
        mock_cfg.return_value = {'mcp_servers': {'web-srv': {'url': 'http://x', 'auth': 'oauth'}}}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        payload = _json_payload(h)
        assert payload['ok'] is False
        assert 'OAuth' in payload['error']


class TestProbeMcpServerBridgeFunction:
    def test_unknown_server_raises_keyerror(self, fake_probe_agent, tmp_path):
        fake_probe_agent()
        with pytest.raises(KeyError):
            bridge.probe_mcp_server('does-not-exist', tmp_path)

    def test_passes_explicit_timeout_to_probe(self, fake_probe_agent, tmp_path):
        fake = fake_probe_agent()
        bridge.probe_mcp_server('web-srv', tmp_path, timeout=7.5)
        assert fake.probed_with == ('web-srv', 7.5)

    def test_default_timeout_does_not_override_server_connect_timeout(self, fake_probe_agent, tmp_path):
        # Regression: an earlier version hard-defaulted to timeout=15.0 and
        # always passed it as connect_timeout, silently overriding a
        # server's own (possibly larger) connect_timeout — a legitimately
        # slow stdio cold-start (e.g. npx) then showed a false "test failed"
        # at 15s instead of the server's configured budget. No explicit
        # timeout must mean no connect_timeout override at all, so
        # _probe_single_server's own per-server resolution (or its 30s
        # fallback) applies instead.
        fake = fake_probe_agent()
        bridge.probe_mcp_server('web-srv', tmp_path)
        assert fake.probed_with == ('web-srv', None)


class TestBrokenBridge503:
    """Agent checkout configured but its config layer fails to import.

    Must fail closed with 503 — same convention as the write routes
    (_mcp_bridge_or_legacy) and _mcp_write_capability's `writable: false`
    case, not silently fall back to the standalone HTTP-reachability probe.
    """

    @pytest.fixture
    def broken_bridge(self, monkeypatch, tmp_path):
        monkeypatch.delenv('HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE', raising=False)
        for name in ('hermes_constants', 'hermes_cli', 'hermes_cli.config'):
            monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setattr(bridge, '_AGENT_DIR', str(tmp_path / 'missing-agent'), raising=False)
        monkeypatch.setattr(bridge, '_import_state', None, raising=False)

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname in ('hermes_constants', 'hermes_cli', 'hermes_cli.config'):
                    raise ImportError(f'{fullname} blocked by test')
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        yield
        sys.meta_path.remove(blocker)
        bridge._import_state = None

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_broken_bridge_returns_503(self, mock_home, mock_cfg, broken_bridge, tmp_path):
        mock_home.return_value = tmp_path
        mock_cfg.return_value = {'mcp_servers': {'web-srv': {'url': 'http://x'}}}
        h = _make_handler()
        _handle_mcp_server_test(h, 'web-srv')
        status = h.send_response.call_args[0][0]
        assert status == 503
        payload = _json_payload(h)
        assert payload['ok'] is False
        assert 'Agent config layer unavailable' in payload['error']
