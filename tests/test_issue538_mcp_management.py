"""Tests for issue #538 — MCP server management API."""
import json, sys, types, pytest
from unittest.mock import patch, MagicMock, call
import yaml
from api.routes import (
    _handle_mcp_servers_list,
    _handle_mcp_server_update,
    _handle_mcp_server_delete,
    _handle_mcp_server_toggle,
    _mask_secrets,
    _mcp_write_capability,
    _parse_mcp_enabled,
    _server_summary,
    _strip_masked_values,
)
from api import agent_config_bridge as bridge
from tests.test_agent_config_bridge import fake_agent  # noqa: F401 — shared fixture, see TestMcpSaveBearerTokenRoundtrip


def _make_handler():
    h = MagicMock()
    h.path = '/api/mcp/servers'
    h.command = 'GET'
    return h


def _json_payload(handler):
    body = handler.wfile.write.call_args[0][0]
    return json.loads(body.decode('utf-8'))


SAMPLE_MCP = {
    "searxng": {
        "command": "mcp-searxng",
        "args": ["--port", "8888"],
        "timeout": 120
    },
    "web-reader": {
        "url": "http://localhost:3001/mcp",
        "timeout": 60,
        "headers": {"Authorization": "Bearer secret123"}
    }
}


class TestMcpList:
    """GET /api/mcp/servers — list with masked secrets."""

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_returns_servers_list(self, mock_home, mock_cfg):
        mock_home.return_value = sentinel_home = object()
        mock_cfg.return_value = {'mcp_servers': SAMPLE_MCP}
        h = _make_handler()
        _handle_mcp_servers_list(h)
        assert h.send_response.called
        status = h.send_response.call_args[0][0]
        assert status == 200
        mock_cfg.assert_called_once_with(sentinel_home)

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_reads_active_profile_home_for_servers(self, mock_home, mock_cfg):
        mock_home.return_value = sentinel_home = object()
        mock_cfg.return_value = {'mcp_servers': {'active': SAMPLE_MCP['searxng']}}
        h = _make_handler()
        _handle_mcp_servers_list(h)
        payload = _json_payload(h)
        assert [srv['name'] for srv in payload['servers']] == ['active']
        mock_cfg.assert_called_once_with(sentinel_home)

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_empty_config(self, mock_home, mock_cfg):
        mock_home.return_value = object()
        mock_cfg.return_value = {}
        h = _make_handler()
        _handle_mcp_servers_list(h)
        assert h.send_response.called
        status = h.send_response.call_args[0][0]
        assert status == 200
        payload = _json_payload(h)
        assert payload['servers'] == []
        assert payload['toggle_supported'] is True
        assert payload['reload_required'] is True

    @patch('api.routes._mcp_runtime_status_by_name')
    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_list_payload_includes_status_tool_counts_and_safe_invalid_config(self, mock_home, mock_cfg, mock_runtime):
        mock_home.return_value = object()
        mock_cfg.return_value = {
            'mcp_servers': {
                'searxng': {'command': 'mcp-searxng', 'args': ['--port', '8888']},
                'web-reader': {
                    'url': 'http://localhost:3001/mcp',
                    'headers': {'Authorization': 'Bearer secret123'},
                },
                'disabled': {'command': 'disabled-cmd', 'enabled': 0},
                'broken': 'not-a-dict',
            }
        }
        mock_runtime.return_value = {
            'searxng': {'connected': True, 'tools': 3},
            'web-reader': {'connected': False, 'tools': 0},
        }
        h = _make_handler()
        _handle_mcp_servers_list(h)
        payload = _json_payload(h)
        by_name = {s['name']: s for s in payload['servers']}
        assert by_name['searxng']['status'] == 'active'
        assert by_name['searxng']['active'] is True
        assert by_name['searxng']['tool_count'] == 3
        assert by_name['web-reader']['status'] == 'configured'
        assert '••••' in by_name['web-reader']['headers']['Authorization']
        assert by_name['disabled']['enabled'] is False
        assert by_name['disabled']['active'] is False
        assert by_name['disabled']['status'] == 'disabled'
        assert by_name['broken']['transport'] == 'invalid'
        assert by_name['broken']['status'] == 'invalid_config'

    def test_secrets_are_masked(self):
        """_mask_secrets hides API keys in headers and env."""
        masked = _mask_secrets(SAMPLE_MCP['web-reader']['headers'])
        assert masked['Authorization'] != 'Bearer secret123'
        assert '••••' in masked['Authorization']

    def test_server_summary_stdio(self):
        summary = _server_summary('searxng', SAMPLE_MCP['searxng'])
        assert summary['transport'] == 'stdio'
        assert summary['command'] == 'mcp-searxng'
        assert summary['args'] == ['--port', '8888']

    def test_server_summary_http(self):
        summary = _server_summary('web-reader', SAMPLE_MCP['web-reader'])
        assert summary['transport'] == 'http'
        assert summary['url'] == 'http://localhost:3001/mcp'
        assert '••••' in summary['headers']['Authorization']

    def test_server_summary_default_timeout(self):
        summary = _server_summary('minimal', {'command': 'x'})
        assert summary['timeout'] == 120

    def test_numeric_zero_enabled_flag_is_disabled(self):
        """YAML numeric false-y values should not show a disabled server as enabled."""
        assert _parse_mcp_enabled(0) is False

    def test_active_home_list_reads_external_config_override_used_by_writes(self, monkeypatch, tmp_path):
        """HERMES_CONFIG_PATH outside the active home remains the read/write authority."""
        from api import config, profiles, routes

        active_home = tmp_path / 'active-home'
        override_path = tmp_path / 'override-dir' / 'config.yaml'
        active_home.mkdir()
        override_path.parent.mkdir()
        active_home.joinpath('config.yaml').write_text(
            yaml.safe_dump({'mcp_servers': {'wrong-home': {'command': 'wrong'}}}, sort_keys=False),
            encoding='utf-8',
        )
        override_path.write_text(
            yaml.safe_dump({'mcp_servers': {'override-srv': {'command': 'override'}}}, sort_keys=False),
            encoding='utf-8',
        )
        monkeypatch.setenv('HERMES_CONFIG_PATH', str(override_path))
        monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: active_home)
        monkeypatch.setattr(routes, 'get_active_hermes_home', lambda: active_home)
        monkeypatch.setattr(routes, '_mcp_runtime_status_by_name', lambda: {})
        config.reload_config()

        h = _make_handler()
        _handle_mcp_servers_list(h)
        payload = _json_payload(h)
        assert [srv['name'] for srv in payload['servers']] == ['override-srv']

        h = _make_handler()
        h.command = 'PUT'
        _handle_mcp_server_update(h, 'new-srv', {'command': 'new-command'})
        saved = yaml.safe_load(override_path.read_text(encoding='utf-8'))
        active_home_saved = yaml.safe_load(active_home.joinpath('config.yaml').read_text(encoding='utf-8'))
        assert 'new-srv' in saved['mcp_servers']
        assert 'new-srv' not in active_home_saved['mcp_servers']


class TestMcpWriteCapability:
    """`writable` field on GET /api/mcp/servers — see routes._mcp_write_capability.

    Convention: a GET response bearing on write capability carries a
    `writable` bool; false only for the specific fail-closed case where an
    agent checkout IS configured but its config layer failed to import (a
    half-wired deployment, distinct from "no checkout at all" which writes
    fine through the legacy YAML path).
    """

    def test_standalone_is_writable(self, monkeypatch):
        # conftest.py sets HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE=1 and no
        # agent dir is configured in the test env — legacy writer applies.
        monkeypatch.setattr(bridge, '_import_state', None, raising=False)
        result = _mcp_write_capability()
        assert result == {'writable': True}

    def test_bridge_available_is_writable(self, monkeypatch, tmp_path):
        hermes_constants = types.ModuleType('hermes_constants')
        hermes_constants.set_hermes_home_override = lambda path: object()
        hermes_constants.reset_hermes_home_override = lambda token: None
        config_mod = types.ModuleType('hermes_cli.config')
        config_mod.load_config = lambda: {}
        config_mod.save_config = lambda cfg, **kw: None
        config_mod.save_env_value = lambda k, v: None
        hermes_cli = types.ModuleType('hermes_cli')
        hermes_cli.__path__ = []
        monkeypatch.delenv('HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE', raising=False)
        monkeypatch.setitem(sys.modules, 'hermes_constants', hermes_constants)
        monkeypatch.setitem(sys.modules, 'hermes_cli', hermes_cli)
        monkeypatch.setitem(sys.modules, 'hermes_cli.config', config_mod)
        monkeypatch.setattr(bridge, '_AGENT_DIR', str(tmp_path / 'agent'), raising=False)
        monkeypatch.setattr(bridge, '_import_state', None, raising=False)
        try:
            result = _mcp_write_capability()
            assert result == {'writable': True}
        finally:
            bridge._import_state = None

    def test_broken_checkout_is_not_writable(self, monkeypatch, tmp_path):
        # Agent checkout configured but its config layer cannot import —
        # must fail closed, not silently fall back to the legacy writer.
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
        try:
            result = _mcp_write_capability()
        finally:
            sys.meta_path.remove(blocker)
            bridge._import_state = None
        assert result['writable'] is False
        assert 'unavailable_reason' in result and result['unavailable_reason']

    @patch('api.routes.get_config_for_profile_home')
    @patch('api.routes.get_active_hermes_home')
    def test_list_payload_carries_write_capability(self, mock_home, mock_cfg, monkeypatch):
        mock_home.return_value = object()
        mock_cfg.return_value = {'mcp_servers': {}}
        monkeypatch.setattr(bridge, '_import_state', None, raising=False)
        h = _make_handler()
        _handle_mcp_servers_list(h)
        payload = _json_payload(h)
        assert payload['writable'] is True
        assert 'unavailable_reason' not in payload


class TestMcpSave:
    """PUT /api/mcp/servers/<name> — add or update."""

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_add_new_stdio_server(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {}
        h = _make_handler()
        h.command = 'PUT'
        body = {"command": "test-cmd", "timeout": 30}
        _handle_mcp_server_update(h, 'test-server', body)
        assert mock_save.called
        saved = mock_save.call_args[0][1]
        assert 'test-server' in saved['mcp_servers']
        assert saved['mcp_servers']['test-server']['command'] == 'test-cmd'

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_add_new_http_server(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {}
        h = _make_handler()
        h.command = 'PUT'
        body = {"url": "http://localhost:4000", "timeout": 60}
        _handle_mcp_server_update(h, 'http-srv', body)
        saved = mock_save.call_args[0][1]
        assert saved['mcp_servers']['http-srv']['url'] == 'http://localhost:4000'

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_update_existing(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'existing': {'command': 'old'}}}
        h = _make_handler()
        h.command = 'PUT'
        body = {"command": "new-cmd"}
        _handle_mcp_server_update(h, 'existing', body)
        saved = mock_save.call_args[0][1]
        assert saved['mcp_servers']['existing']['command'] == 'new-cmd'

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_preserves_other_servers(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'keep': {'command': 'stay'}}}
        h = _make_handler()
        h.command = 'PUT'
        body = {"command": "new"}
        _handle_mcp_server_update(h, 'add-me', body)
        saved = mock_save.call_args[0][1]
        assert 'keep' in saved['mcp_servers']
        assert 'add-me' in saved['mcp_servers']

    def test_empty_name_rejected(self):
        h = _make_handler()
        h.command = 'PUT'
        _handle_mcp_server_update(h, '', {"command": "test"})
        assert h.send_response.called
        status = h.send_response.call_args[0][0]
        assert status == 400

    def test_missing_command_and_url_rejected(self):
        h = _make_handler()
        h.command = 'PUT'
        _handle_mcp_server_update(h, 'test', {"timeout": 30})
        assert h.send_response.called
        status = h.send_response.call_args[0][0]
        assert status == 400


class TestMcpSaveBridgeModeMasking:
    """Bridge-mode PUT — existing_cfg for _strip_masked_values() must come
    from the bridge's own home-scoped reader (_bridge.load_agent_config),
    not the WebUI's get_config() cache. The two can diverge (stale cache,
    different profile resolution); if existing_cfg is read from the wrong
    source, a masked •••••• field submitted unchanged would be missed and
    saved as the literal placeholder — a quiet secret loss. Uses the same
    sys.modules-faking pattern as tests/test_agent_config_bridge.py.
    """

    def _activate_fake_agent(self, monkeypatch, tmp_path, initial_config):
        from api import agent_config_bridge as bridge

        state = {"config": initial_config}

        hermes_constants = types.ModuleType("hermes_constants")
        hermes_constants.set_hermes_home_override = lambda path: object()
        hermes_constants.reset_hermes_home_override = lambda token: None

        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = lambda: {k: v for k, v in state["config"].items()}

        def _save_config(cfg, **kwargs):
            state["config"] = dict(cfg)

        config_mod.save_config = _save_config
        config_mod.save_env_value = lambda k, v: None

        security_mod = types.ModuleType("hermes_cli.mcp_security")
        security_mod.validate_mcp_server_entry = lambda name, entry: []

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []

        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
        monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
        monkeypatch.setitem(sys.modules, "hermes_cli.mcp_security", security_mod)
        monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        return state

    @patch('api.routes.reload_config')
    @patch('api.routes.get_active_hermes_home')
    @patch('api.routes.get_config')
    def test_masked_header_survives_timeout_only_edit(
        self, mock_get_config, mock_home, mock_reload, monkeypatch, tmp_path
    ):
        from api import agent_config_bridge as bridge

        real_headers = {"Authorization": "Bearer real-secret-token"}
        initial_config = {
            "mcp_servers": {
                "web-srv": {
                    "url": "http://localhost:4000",
                    "headers": dict(real_headers),
                    "timeout": 60,
                },
            }
        }
        state = self._activate_fake_agent(monkeypatch, tmp_path, initial_config)
        mock_home.return_value = tmp_path
        # The WebUI's own get_config() cache is made to DIVERGE from the
        # bridge's real config (simulating a stale cache / different profile
        # resolution) — this is the exact condition the fix must be immune
        # to: existing_cfg must come from the bridge, not from here.
        mock_get_config.return_value = {"mcp_servers": {}}

        h = _make_handler()
        h.command = 'PUT'
        # Real UI behavior: the GET response masked the header to ••••••,
        # and editing the form (changing only the timeout) round-trips the
        # masked placeholder unchanged.
        body = {
            "url": "http://localhost:4000",
            "headers": {"Authorization": "••••••"},
            "timeout": 120,
        }
        _handle_mcp_server_update(h, 'web-srv', body)

        status = h.send_response.call_args[0][0]
        assert status == 200
        saved = state["config"]["mcp_servers"]["web-srv"]
        assert saved["headers"]["Authorization"] == "Bearer real-secret-token"
        assert saved["timeout"] == 120

        bridge._import_state = None


class TestMcpDelete:
    """DELETE /api/mcp/servers/<name>."""

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_delete_existing(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'target': {'command': 'rm'}}}
        h = _make_handler()
        h.command = 'DELETE'
        _handle_mcp_server_delete(h, 'target')
        assert mock_save.called
        saved = mock_save.call_args[0][1]
        assert 'target' not in saved.get('mcp_servers', {})

    @patch('api.routes.get_config')
    def test_delete_nonexistent(self, mock_cfg):
        mock_cfg.return_value = {'mcp_servers': {}}
        h = _make_handler()
        h.command = 'DELETE'
        _handle_mcp_server_delete(h, 'ghost')
        status = h.send_response.call_args[0][0]
        assert status == 404

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_preserves_others(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'a': {'c': '1'}, 'b': {'c': '2'}}}
        h = _make_handler()
        h.command = 'DELETE'
        _handle_mcp_server_delete(h, 'a')
        saved = mock_save.call_args[0][1]
        assert 'a' not in saved['mcp_servers']
        assert 'b' in saved['mcp_servers']

    def test_empty_name_rejected(self):
        h = _make_handler()
        h.command = 'DELETE'
        _handle_mcp_server_delete(h, '')
        status = h.send_response.call_args[0][0]
        assert status == 400


class TestMaskSecrets:
    """Unit tests for _mask_secrets helper."""

    def test_masks_env_values(self):
        obj = {"env": {"API_KEY": "***", "PUBLIC_VAR": "visible"}}
        result = _mask_secrets(obj)
        assert result["env"]["API_KEY"] == "••••••"
        assert result["env"]["PUBLIC_VAR"] == "visible"

    def test_masks_headers(self):
        obj = {"headers": {"Authorization": "Bearer token", "Accept": "application/json"}}
        result = _mask_secrets(obj)
        assert "••••" in result["headers"]["Authorization"]
        assert result["headers"]["Accept"] == "application/json"

    def test_passes_non_dict(self):
        assert _mask_secrets("hello") == "hello"
        assert _mask_secrets(42) == 42
        assert _mask_secrets(None) is None

    def test_handles_empty_dict(self):
        assert _mask_secrets({}) == {}

    def test_masks_password_key(self):
        obj = {"password": "hunter2"}
        result = _mask_secrets(obj)
        assert result["password"] == "••••••"


class TestStripMaskedValues:
    """Unit tests for _strip_masked_values helper (secret round-trip protection)."""

    def test_masked_env_preserves_original(self):
        """Submitting masked env value should keep the original stored value."""
        existing = {"API_KEY": "real-secret-123", "PUBLIC": "visible"}
        submitted = {"API_KEY": "••••••", "PUBLIC": "updated"}
        result = _strip_masked_values(submitted, existing)
        assert result["API_KEY"] == "real-secret-123"
        assert result["PUBLIC"] == "updated"

    def test_masked_headers_preserves_original(self):
        """Submitting masked header value should keep the original stored value."""
        existing = {"Authorization": "Bearer token123", "Accept": "application/json"}
        submitted = {"Authorization": "••••••", "Accept": "text/html"}
        result = _strip_masked_values(submitted, existing)
        assert result["Authorization"] == "Bearer token123"
        assert result["Accept"] == "text/html"

    def test_new_key_still_saved(self):
        """New keys (not in existing) should be saved even if they look sensitive."""
        existing = {"OLD_KEY": "old"}
        submitted = {"NEW_KEY": "new-value", "OLD_KEY": "••••••"}
        result = _strip_masked_values(submitted, existing)
        assert result["OLD_KEY"] == "old"
        assert result["NEW_KEY"] == "new-value"

    def test_non_dict_passthrough(self):
        assert _strip_masked_values("hello", {}) == "hello"
        assert _strip_masked_values(42, {}) == 42

    def test_empty_dicts(self):
        assert _strip_masked_values({}, {}) == {}
        assert _strip_masked_values({"k": "v"}, {}) == {"k": "v"}


class TestMcpToggle:
    """PATCH /api/mcp/servers/<name> — enable/disable."""

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_disable_server(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'myserver': {'command': 'run'}}}
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'myserver', {'enabled': False})
        assert mock_save.called
        saved = mock_save.call_args[0][1]
        assert saved['mcp_servers']['myserver']['enabled'] is False
        assert mock_reload.called

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_enable_server(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'myserver': {'command': 'run', 'enabled': False}}}
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'myserver', {'enabled': True})
        saved = mock_save.call_args[0][1]
        assert saved['mcp_servers']['myserver']['enabled'] is True

    @patch('api.routes.get_config')
    def test_nonexistent_server_returns_404(self, mock_cfg):
        mock_cfg.return_value = {'mcp_servers': {}}
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'ghost', {'enabled': True})
        status = h.send_response.call_args[0][0]
        assert status == 404

    def test_empty_name_rejected(self):
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, '', {'enabled': True})
        status = h.send_response.call_args[0][0]
        assert status == 400

    def test_missing_enabled_field_rejected(self):
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'myserver', {})
        status = h.send_response.call_args[0][0]
        assert status == 400

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_response_payload(self, mock_cfg, mock_path, mock_save, mock_reload):
        mock_cfg.return_value = {'mcp_servers': {'srv': {'url': 'http://localhost'}}}
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'srv', {'enabled': False})
        body = h.wfile.write.call_args[0][0]
        payload = json.loads(body.decode('utf-8'))
        assert payload == {'ok': True, 'name': 'srv', 'enabled': False}

    @patch('api.routes.reload_config')
    @patch('api.routes._save_yaml_config_file')
    @patch('api.routes._get_config_path', return_value='/tmp/test.yaml')
    @patch('api.routes.get_config')
    def test_url_encoded_name(self, mock_cfg, mock_path, mock_save, mock_reload):
        """Names with special characters must be URL-decoded."""
        mock_cfg.return_value = {'mcp_servers': {'my server': {'command': 'x'}}}
        h = _make_handler()
        h.command = 'PATCH'
        _handle_mcp_server_toggle(h, 'my%20server', {'enabled': False})
        saved = mock_save.call_args[0][1]
        assert 'my server' in saved['mcp_servers']
        assert saved['mcp_servers']['my server']['enabled'] is False
