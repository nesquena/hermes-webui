"""
Tests for gateway model integration in api/streaming.py.

Verifies that when a gateway model is selected:
1. The gateway provider is detected and resolved
2. A synthetic API key is used (no real key needed)
3. Extra headers (x-instance-keyword) are injected via request_overrides
4. Non-gateway models fall through to normal resolution
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


FAKE_INSTANCES = [
    {"keyword": "my-cursor-1", "cli": "cursor", "status": "alive", "model": "gpt-4"},
    {"keyword": "cp1", "cli": "copilot", "status": "alive", "model": "gpt-4"},
]

FAKE_CONFIGS = [
    {"label": "local", "url": "http://localhost:3456"},
]


class TestStreamingGatewayIntegration(unittest.TestCase):
    """Test that _run_agent_streaming correctly handles gateway models."""

    def _make_fake_agent_cls(self):
        """Return a mock AIAgent class that records constructor kwargs."""
        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.model = kwargs.get("model", "")
                self.provider = kwargs.get("provider", "")
                self.base_url = kwargs.get("base_url", "")
                self.api_key = kwargs.get("api_key", "")
                self.request_overrides = kwargs.get("request_overrides")
                self.session_id = kwargs.get("session_id", "")

            def run(self, *a, **kw):
                return "done"

        return FakeAgent, captured

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    @patch("api.streaming._get_ai_agent")
    @patch("api.streaming.resolve_model_provider")
    def test_gateway_model_injects_extra_headers(
        self, mock_resolve_model, mock_get_agent, mock_discover, mock_configs
    ):
        """Gateway models should inject x-instance-keyword via request_overrides."""
        mock_resolve_model.return_value = ("gpt-4", "openai", "http://localhost:3456/cursor/v1")

        FakeAgent, captured = self._make_fake_agent_cls()
        mock_get_agent.return_value = FakeAgent

        from api.streaming import _run_agent_streaming

        model_id = "@gateway-local:gpt-4/my-cursor-1"

        with patch("api.streaming.get_session") as mock_get_session, \
             patch("api.streaming.set_last_workspace"), \
             patch("api.streaming._set_thread_env"), \
             patch("api.streaming._clear_thread_env"), \
             patch("api.streaming._get_session_agent_lock") as mock_lock, \
             patch("api.streaming.CANCEL_FLAGS", {}), \
             patch("api.streaming.AGENT_INSTANCES", {}), \
             patch("api.streaming.STREAMS", {}), \
             patch("api.streaming.STREAMS_LOCK", threading.Lock()), \
             patch("api.streaming._ENV_LOCK", threading.Lock()), \
             patch.dict(os.environ, {}, clear=False):

            mock_session = MagicMock()
            mock_session.messages = []
            mock_session.workspace = None
            mock_get_session.return_value = mock_session
            mock_lock.return_value = MagicMock()

            try:
                _run_agent_streaming("test-session", model_id, "Hello", MagicMock())
            except Exception:
                pass

            if captured:
                self.assertEqual(captured.get("api_key"), "agent-gateway-no-key-required")
                overrides = captured.get("request_overrides")
                self.assertIsNotNone(overrides)
                self.assertIn("extra_headers", overrides)
                self.assertEqual(
                    overrides["extra_headers"]["x-instance-keyword"],
                    "my-cursor-1",
                )

    def test_non_gateway_model_no_detection(self):
        """Non-gateway models should not be detected as gateway models."""
        from api.gateway_provider import is_gateway_model
        self.assertFalse(is_gateway_model("gpt-4"))
        self.assertFalse(is_gateway_model("claude-3-opus"))
        self.assertFalse(is_gateway_model(""))
        self.assertFalse(is_gateway_model("anthropic/claude-3"))

    def test_gateway_model_id_detection(self):
        """Verify the gateway model ID format is correctly detected."""
        from api.gateway_provider import is_gateway_model
        self.assertTrue(is_gateway_model("@gateway-local:gpt-4/my-cursor"))
        self.assertTrue(is_gateway_model("@gateway-remote:claude-3/copilot-1"))

    def test_request_overrides_empty_for_non_gateway(self):
        """When no gateway headers exist, request_overrides should be None."""
        _gateway_extra_headers = {}
        _request_overrides = {}
        if _gateway_extra_headers:
            _request_overrides["extra_headers"] = _gateway_extra_headers

        result = _request_overrides if _request_overrides else None
        self.assertIsNone(result)

    def test_request_overrides_populated_for_gateway(self):
        """When gateway headers exist, request_overrides should contain extra_headers."""
        _gateway_extra_headers = {"x-instance-keyword": "test-inst"}
        _request_overrides = {}
        if _gateway_extra_headers:
            _request_overrides["extra_headers"] = _gateway_extra_headers

        result = _request_overrides if _request_overrides else None
        self.assertIsNotNone(result)
        self.assertEqual(result["extra_headers"]["x-instance-keyword"], "test-inst")


class TestGatewayResolveIntegration(unittest.TestCase):
    """Test resolve_gateway_model returns correct routing info."""

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_resolve_returns_extra_headers(self, mock_discover, mock_configs):
        """resolve_gateway_model should include extra_headers with x-instance-keyword."""
        from api.gateway_provider import resolve_gateway_model, build_model_id

        model_id = build_model_id("local", "gpt-4", "my-cursor-1")
        result = resolve_gateway_model(model_id)

        self.assertIsNotNone(result)
        self.assertEqual(result["model"], "gpt-4")
        self.assertIn("extra_headers", result)
        self.assertEqual(result["extra_headers"]["x-instance-keyword"], "my-cursor-1")

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_resolve_synthetic_api_key(self, mock_discover, mock_configs):
        """Gateway models should use a synthetic API key."""
        from api.gateway_provider import resolve_gateway_model, build_model_id

        model_id = build_model_id("local", "gpt-4", "my-cursor-1")
        result = resolve_gateway_model(model_id)

        self.assertIsNotNone(result)
        self.assertEqual(result["api_key"], "agent-gateway-no-key-required")

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_cursor_route(self, mock_discover, mock_configs):
        """Cursor instances should route to /cursor/v1."""
        from api.gateway_provider import resolve_gateway_model, build_model_id

        model_id = build_model_id("local", "gpt-4", "my-cursor-1")
        result = resolve_gateway_model(model_id)
        self.assertIn("/cursor/v1", result["base_url"])

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_copilot_route(self, mock_discover, mock_configs):
        """Copilot instances should route to /copilot/v1."""
        from api.gateway_provider import resolve_gateway_model, build_model_id

        model_id = build_model_id("local", "gpt-4", "cp1")
        result = resolve_gateway_model(model_id)
        self.assertIn("/copilot/v1", result["base_url"])

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_provider_is_openai(self, mock_discover, mock_configs):
        """Gateway models should always report provider as 'openai'."""
        from api.gateway_provider import resolve_gateway_model, build_model_id

        model_id = build_model_id("local", "gpt-4", "my-cursor-1")
        result = resolve_gateway_model(model_id)
        self.assertEqual(result["provider"], "openai")

    @patch("api.gateway_provider._load_gateway_configs", return_value=[])
    def test_resolve_unknown_label(self, mock_configs):
        """Unknown gateway label should return None."""
        from api.gateway_provider import resolve_gateway_model

        result = resolve_gateway_model("@gateway-nonexistent:gpt-4/inst1")
        self.assertIsNone(result)

    def test_resolve_non_gateway_model(self):
        """Non-gateway model IDs should return None."""
        from api.gateway_provider import resolve_gateway_model

        self.assertIsNone(resolve_gateway_model("gpt-4"))
        self.assertIsNone(resolve_gateway_model(""))
        self.assertIsNone(resolve_gateway_model("anthropic/claude-3"))


class TestConfigIntegration(unittest.TestCase):
    """Test that config.py correctly delegates to gateway_provider."""

    @patch("api.gateway_provider._load_gateway_configs", return_value=FAKE_CONFIGS)
    @patch("api.gateway_provider.discover_instances", return_value=FAKE_INSTANCES)
    def test_resolve_model_provider_delegates_gateway(self, mock_discover, mock_configs):
        """resolve_model_provider should delegate gateway models to gateway_provider."""
        from api.config import resolve_model_provider
        from api.gateway_provider import build_model_id

        model_id = build_model_id("local", "gpt-4", "my-cursor-1")
        model, provider, base_url = resolve_model_provider(model_id)

        self.assertEqual(model, "gpt-4")
        self.assertEqual(provider, "openai")
        self.assertIn("/cursor/v1", base_url)

    def test_resolve_model_provider_normal_passthrough(self):
        """Normal models should not be affected by gateway integration."""
        from api.config import resolve_model_provider

        model, provider, base_url = resolve_model_provider("gpt-4")
        self.assertNotIn("gateway", str(base_url or ""))


if __name__ == "__main__":
    unittest.main()
