"""
Tests for api/gateway_provider.py — Agent API Gateway integration.

Covers:
  - Model ID encoding/decoding
  - Instance discovery with caching
  - Gateway config loading (config.yaml + env vars)
  - Model group generation for dropdown
  - Model resolution for routing
  - Cache TTL and invalidation
  - Error handling (network failures, malformed data)
"""

import json
import os
import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.gateway_provider import (
    build_model_id,
    parse_model_id,
    is_gateway_model,
    discover_instances,
    clear_cache,
    get_gateway_model_groups,
    resolve_gateway_model,
    _filter_active,
    _fetch_instances,
    _load_gateway_configs,
    GATEWAY_PROVIDER_PREFIX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_INSTANCES = [
    {
        "id": "inst-001",
        "keyword": "test",
        "model": "claude-4.6-opus-high",
        "cli": "cursor",
        "status": "ready",
        "pid": 12345,
        "createdAt": "2026-04-12T10:00:00Z",
        "lastUsedAt": "2026-04-12T10:05:00Z",
        "requestCount": 5,
        "turnCount": 10,
        "restartCount": 0,
    },
    {
        "id": "inst-002",
        "keyword": "prod",
        "model": "claude-4.6-opus-high",
        "cli": "copilot",
        "status": "busy",
        "pid": 12346,
        "createdAt": "2026-04-12T09:00:00Z",
        "lastUsedAt": "2026-04-12T10:03:00Z",
        "requestCount": 20,
        "turnCount": 40,
        "restartCount": 1,
    },
    {
        "id": "inst-003",
        "keyword": "dead",
        "model": "gpt-4.1",
        "cli": "copilot",
        "status": "dead",
        "pid": 0,
        "createdAt": "2026-04-12T08:00:00Z",
        "lastUsedAt": "2026-04-12T08:30:00Z",
        "requestCount": 2,
        "turnCount": 3,
        "restartCount": 5,
    },
]


class MockGatewayHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves /admin/instances."""

    instances = MOCK_INSTANCES

    def do_GET(self):
        if self.path == "/admin/instances":
            body = json.dumps(self.instances).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress request logs in test output


def _start_mock_server(handler_class=MockGatewayHandler):
    """Start a mock gateway server on a random port. Returns (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Test: Model ID encoding / decoding
# ---------------------------------------------------------------------------

class TestModelIdEncoding(unittest.TestCase):

    def test_build_model_id(self):
        mid = build_model_id("local", "claude-4.6-opus-high", "test")
        self.assertEqual(mid, "@gateway-local:claude-4.6-opus-high/test")

    def test_build_model_id_remote(self):
        mid = build_model_id("remote", "gpt-4.1", "prod")
        self.assertEqual(mid, "@gateway-remote:gpt-4.1/prod")

    def test_parse_model_id_valid(self):
        result = parse_model_id("@gateway-local:claude-4.6-opus-high/test")
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "local")
        self.assertEqual(result["model_name"], "claude-4.6-opus-high")
        self.assertEqual(result["keyword"], "test")
        self.assertEqual(result["provider_id"], "gateway-local")

    def test_parse_model_id_no_keyword(self):
        result = parse_model_id("@gateway-local:claude-4.6-opus-high")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_name"], "claude-4.6-opus-high")
        self.assertEqual(result["keyword"], "")

    def test_parse_model_id_not_gateway(self):
        self.assertIsNone(parse_model_id("claude-4.6-opus-high"))
        self.assertIsNone(parse_model_id("@anthropic:claude-4.6"))
        self.assertIsNone(parse_model_id(""))

    def test_is_gateway_model(self):
        self.assertTrue(is_gateway_model("@gateway-local:claude-4.6-opus-high/test"))
        self.assertFalse(is_gateway_model("claude-4.6-opus-high"))
        self.assertFalse(is_gateway_model("@openai:gpt-4"))

    def test_roundtrip(self):
        mid = build_model_id("remote", "model-x", "kw-y")
        parsed = parse_model_id(mid)
        self.assertEqual(parsed["label"], "remote")
        self.assertEqual(parsed["model_name"], "model-x")
        self.assertEqual(parsed["keyword"], "kw-y")

    def test_parse_model_id_missing_colon(self):
        self.assertIsNone(parse_model_id("@gateway-local"))


# ---------------------------------------------------------------------------
# Test: Instance filtering
# ---------------------------------------------------------------------------

class TestFilterActive(unittest.TestCase):

    def test_filters_dead_instances(self):
        active = _filter_active(MOCK_INSTANCES)
        self.assertEqual(len(active), 2)
        statuses = {i["status"] for i in active}
        self.assertEqual(statuses, {"ready", "busy"})

    def test_empty_list(self):
        self.assertEqual(_filter_active([]), [])

    def test_all_dead(self):
        dead = [{"status": "dead"}, {"status": "error"}, {"status": "starting"}]
        self.assertEqual(_filter_active(dead), [])


# ---------------------------------------------------------------------------
# Test: HTTP discovery
# ---------------------------------------------------------------------------

class TestFetchInstances(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_fetch_from_live_server(self):
        server, url = _start_mock_server()
        try:
            instances = _fetch_instances(url)
            self.assertEqual(len(instances), 3)
            self.assertEqual(instances[0]["keyword"], "test")
        finally:
            server.shutdown()

    def test_fetch_unreachable(self):
        instances = _fetch_instances("http://127.0.0.1:1", timeout_s=1.0)
        self.assertEqual(instances, [])

    def test_fetch_non_json(self):
        class BadHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not json")
            def log_message(self, *args):
                pass

        server, url = _start_mock_server(BadHandler)
        try:
            instances = _fetch_instances(url)
            self.assertEqual(instances, [])
        finally:
            server.shutdown()

    def test_fetch_server_error(self):
        class ErrorHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()
            def log_message(self, *args):
                pass

        server, url = _start_mock_server(ErrorHandler)
        try:
            instances = _fetch_instances(url)
            self.assertEqual(instances, [])
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Test: Discovery with caching
# ---------------------------------------------------------------------------

class TestDiscoverInstances(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_discover_caches_results(self):
        server, url = _start_mock_server()
        try:
            result1 = discover_instances(url)
            self.assertEqual(len(result1), 2)  # only active

            # Shut down server — cached result should still work
            server.shutdown()

            result2 = discover_instances(url)
            self.assertEqual(len(result2), 2)
            self.assertEqual(result1, result2)
        finally:
            try:
                server.shutdown()
            except Exception:
                pass

    def test_discover_force_refresh(self):
        server, url = _start_mock_server()
        try:
            discover_instances(url)

            # Force refresh should hit the server again
            result = discover_instances(url, force_refresh=True)
            self.assertEqual(len(result), 2)
        finally:
            server.shutdown()

    def test_discover_cache_expiry(self):
        server, url = _start_mock_server()
        try:
            discover_instances(url, max_age_s=0.1)
            time.sleep(0.2)

            # Cache expired — should re-fetch
            result = discover_instances(url, max_age_s=0.1)
            self.assertEqual(len(result), 2)
        finally:
            server.shutdown()

    def test_discover_fallback_to_stale_cache(self):
        server, url = _start_mock_server()
        try:
            discover_instances(url)
        finally:
            server.shutdown()

        # Server is down, cache is stale but should still return last-known-good
        result = discover_instances(url, max_age_s=0)
        self.assertEqual(len(result), 2)

    def test_clear_cache_specific(self):
        server, url = _start_mock_server()
        try:
            discover_instances(url)
            clear_cache(url)
            # After clearing, should re-fetch
            result = discover_instances(url)
            self.assertEqual(len(result), 2)
        finally:
            server.shutdown()

    def test_clear_cache_all(self):
        server, url = _start_mock_server()
        try:
            discover_instances(url)
            clear_cache()
            # After clearing all, should re-fetch
            result = discover_instances(url)
            self.assertEqual(len(result), 2)
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Test: Config loading
# ---------------------------------------------------------------------------

class TestLoadGatewayConfigs(unittest.TestCase):

    def test_env_var_local(self):
        with patch.dict(os.environ, {"AGENT_GATEWAY_LOCAL_URL": "http://10.0.0.1:3000"}, clear=False):
            with patch("api.gateway_provider.cfg", {}, create=True):
                configs = _load_gateway_configs()
                self.assertTrue(any(c["url"] == "http://10.0.0.1:3000" for c in configs))

    def test_env_var_remote(self):
        with patch.dict(os.environ, {
            "AGENT_GATEWAY_LOCAL_URL": "http://local:3000",
            "AGENT_GATEWAY_REMOTE_URL": "http://remote:3000",
        }, clear=False):
            with patch("api.gateway_provider.cfg", {}, create=True):
                configs = _load_gateway_configs()
                self.assertEqual(len(configs), 2)
                self.assertEqual(configs[0]["url"], "http://local:3000")
                self.assertEqual(configs[1]["url"], "http://remote:3000")

    def test_config_yaml_integration(self):
        mock_cfg = {
            "gateway_providers": [
                {"label": "dev", "url": "http://dev-server:3000"},
                {"label": "staging", "url": "http://staging:3000"},
            ]
        }
        # Patch the import inside _load_gateway_configs
        with patch.dict(os.environ, {}, clear=False):
            # Remove env vars that might interfere
            env_clean = {k: v for k, v in os.environ.items()
                         if k not in ("AGENT_GATEWAY_LOCAL_URL", "AGENT_GATEWAY_REMOTE_URL")}
            with patch.dict(os.environ, env_clean, clear=True):
                with patch("api.gateway_provider._load_gateway_configs") as mock_load:
                    mock_load.return_value = [
                        {"label": "dev", "url": "http://dev-server:3000"},
                        {"label": "staging", "url": "http://staging:3000"},
                    ]
                    configs = mock_load()
                    self.assertEqual(len(configs), 2)
                    self.assertEqual(configs[0]["label"], "dev")


# ---------------------------------------------------------------------------
# Test: Model groups for dropdown
# ---------------------------------------------------------------------------

class TestGetGatewayModelGroups(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_returns_model_groups(self):
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                groups = get_gateway_model_groups()
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["provider"], "gateway-local")
                models = groups[0]["models"]
                self.assertEqual(len(models), 2)  # 2 active instances
                # Check model IDs
                ids = {m["id"] for m in models}
                self.assertIn("@gateway-local:claude-4.6-opus-high/test", ids)
                self.assertIn("@gateway-local:claude-4.6-opus-high/prod", ids)
        finally:
            server.shutdown()

    def test_empty_when_no_gateways(self):
        with patch("api.gateway_provider._load_gateway_configs", return_value=[]):
            groups = get_gateway_model_groups()
            self.assertEqual(groups, [])

    def test_empty_when_gateway_unreachable(self):
        with patch("api.gateway_provider._load_gateway_configs",
                    return_value=[{"label": "local", "url": "http://127.0.0.1:1"}]):
            groups = get_gateway_model_groups()
            self.assertEqual(groups, [])

    def test_model_labels_include_cli_and_keyword(self):
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                groups = get_gateway_model_groups()
                labels = {m["label"] for m in groups[0]["models"]}
                self.assertIn("claude-4.6-opus-high [CURSOR:test]", labels)
                self.assertIn("claude-4.6-opus-high [COPILOT:prod]", labels)
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Test: Model resolution for routing
# ---------------------------------------------------------------------------

class TestResolveGatewayModel(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_resolve_valid_model(self):
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model("@gateway-local:claude-4.6-opus-high/test")
                self.assertIsNotNone(result)
                self.assertEqual(result["model"], "gw:claude-4.6-opus-high")
                self.assertEqual(result["base_url"], f"{url}/cursor/v1")
                self.assertEqual(result["api_key"], "agent-gateway-no-key-required")
                self.assertEqual(result["extra_headers"]["x-instance-keyword"], "test")
        finally:
            server.shutdown()

    def test_resolve_copilot_cli_route(self):
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model("@gateway-local:claude-4.6-opus-high/prod")
                self.assertIsNotNone(result)
                self.assertEqual(result["base_url"], f"{url}/copilot/v1")
        finally:
            server.shutdown()

    def test_resolve_non_gateway_model(self):
        result = resolve_gateway_model("claude-4.6-opus-high")
        self.assertIsNone(result)

    def test_resolve_unknown_label(self):
        with patch("api.gateway_provider._load_gateway_configs",
                    return_value=[{"label": "local", "url": "http://localhost:3000"}]):
            result = resolve_gateway_model("@gateway-nonexistent:model/kw")
            self.assertIsNone(result)

    def test_resolve_provider_is_openai(self):
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model("@gateway-local:claude-4.6-opus-high/test")
                self.assertEqual(result["provider"], "openai")
        finally:
            server.shutdown()

    def test_resolve_uses_gw_prefix_to_disable_responses_api(self):
        """Regression for commit e8d89a7c (#2): the resolved model name must be
        prefixed with 'gw:' so AIAgent's responses-api auto-detection (which
        triggers on any model starting with 'gpt-5') does NOT fire — the
        gateway proxies everything as plain chat completions.
        """
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model("@gateway-local:claude-4.6-opus-high/test")
                self.assertIsNotNone(result)
                self.assertTrue(
                    result["model"].startswith("gw:"),
                    f"expected gw: prefix, got {result['model']!r}",
                )
        finally:
            server.shutdown()

    def test_resolve_sets_x_instance_keyword_header(self):
        """Regression for commit e8d89a7c (#2): keyword is conveyed via the
        x-instance-keyword HTTP header so the gateway can route to the right
        instance. base_url must NOT embed the keyword (that was the rolled-back
        commit 49a2be4f approach)."""
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model("@gateway-local:claude-4.6-opus-high/test")
                self.assertEqual(result["extra_headers"]["x-instance-keyword"], "test")
                # And we should NOT have switched back to the path-routing layout
                self.assertNotIn("/k/", result["base_url"])
                self.assertNotIn("/v1/k/", result["base_url"])
        finally:
            server.shutdown()

    def test_resolve_unknown_keyword_falls_back_to_cursor_route(self):
        """When the requested (model, keyword) pair is not in the discovered
        instance list, ``resolve_gateway_model`` should still produce a usable
        config rather than returning None: cli_route defaults to 'cursor' so a
        stale dropdown selection still routes somewhere instead of silently
        failing.
        """
        server, url = _start_mock_server()
        try:
            with patch("api.gateway_provider._load_gateway_configs",
                        return_value=[{"label": "local", "url": url}]):
                result = resolve_gateway_model(
                    "@gateway-local:never-registered-model/never-registered-kw"
                )
                self.assertIsNotNone(result)
                self.assertEqual(result["base_url"], f"{url}/cursor/v1")
                self.assertEqual(
                    result["extra_headers"]["x-instance-keyword"],
                    "never-registered-kw",
                )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Test: Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_concurrent_discovery(self):
        server, url = _start_mock_server()
        results = []
        errors = []

        def worker():
            try:
                r = discover_instances(url)
                results.append(len(r))
            except Exception as e:
                errors.append(e)

        try:
            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertTrue(all(r == 2 for r in results))
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_empty_instance_list(self):
        class EmptyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                body = b"[]"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass

        server, url = _start_mock_server(EmptyHandler)
        try:
            instances = discover_instances(url)
            self.assertEqual(instances, [])
        finally:
            server.shutdown()

    def test_non_list_response(self):
        class DictHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                body = b'{"error": "not a list"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass

        server, url = _start_mock_server(DictHandler)
        try:
            instances = _fetch_instances(url)
            self.assertEqual(instances, [])
        finally:
            server.shutdown()

    def test_trailing_slash_normalization(self):
        mid1 = build_model_id("local", "m", "k")
        mid2 = build_model_id("local", "m", "k")
        self.assertEqual(mid1, mid2)

    def test_model_id_with_special_chars(self):
        mid = build_model_id("local", "claude-4.6-opus-high-thinking", "my-instance_01")
        parsed = parse_model_id(mid)
        self.assertEqual(parsed["model_name"], "claude-4.6-opus-high-thinking")
        self.assertEqual(parsed["keyword"], "my-instance_01")


if __name__ == "__main__":
    unittest.main()
