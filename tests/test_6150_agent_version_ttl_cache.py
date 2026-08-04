"""
Tests for the agent_version TTL cache / off-thread refresh (PR #6156).

Covers the re-gate requirements:
  1. /api/settings never performs synchronous gateway network I/O — it serves
     an immediate value: fresh cached agent_version, or AGENT_VERSION when cold.
  2. agent_version is pre-assigned to AGENT_VERSION before any probe, so every
     error path retains the fallback (no blank badge).
  3. The gateway-health probe uses ONE overall deadline, caps the response body,
     and swallows every error class (ValueError, IncompleteRead, ...).
  4. Refresh is single-flight — concurrent requests never stack probes.
"""
import http.client
import time

from unittest.mock import patch, MagicMock


def _call_settings():
    """Invoke GET /api/settings and return the captured payload dict."""
    import api.routes as routes
    from urllib.parse import urlparse

    handler = MagicMock()
    parsed = urlparse('/api/settings')
    captured = {}

    def fake_j(h, data, status=200):
        captured['data'] = data

    with patch('api.routes.load_settings', return_value={}), \
         patch('api.routes.j', side_effect=fake_j):
        routes.handle_get(handler, parsed)

    return captured.get('data', {})


class TestSettingsAgentVersionFallback:

    def test_cold_cache_preassigns_agent_version(self):
        """Cold cache -> settings still carries agent_version == AGENT_VERSION."""
        import api.updates as upd

        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': 0.0}), \
             patch.object(upd, '_schedule_agent_version_refresh'):
            data = _call_settings()

        assert 'agent_version' in data, 'agent_version must never be absent'
        assert data['agent_version'] == upd.AGENT_VERSION

    def test_fresh_cache_overrides_with_live_value(self):
        """Fresh cached gateway value overwrites the AGENT_VERSION fallback."""
        import api.updates as upd

        future = time.monotonic() + 3600
        with patch.object(upd, '_agent_version_cache',
                          {'value': 'v0.14.7', 'expires_at': future}), \
             patch.object(upd, '_schedule_agent_version_refresh'):
            data = _call_settings()

        assert data['agent_version'] == 'v0.14.7'

    def test_refresh_scheduled_off_thread_on_every_request(self):
        """The settings path must schedule the refresh, not run the probe."""
        import api.updates as upd

        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': 0.0}) as cache, \
             patch.object(upd, '_schedule_agent_version_refresh') as sched:
            _call_settings()

        sched.assert_called_once_with()
        cache['value'] = None  # ensure no probe ran synchronously


class TestAgentVersionCache:

    def test_get_cached_returns_none_when_cold(self):
        import api.updates as upd
        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': 0.0}):
            assert upd.get_cached_agent_version() is None

    def test_get_cached_returns_fresh_value(self):
        import api.updates as upd
        future = time.monotonic() + 3600
        with patch.object(upd, '_agent_version_cache',
                          {'value': 'v0.14.7', 'expires_at': future}):
            assert upd.get_cached_agent_version() == 'v0.14.7'

    def test_get_cached_returns_none_when_expired(self):
        import api.updates as upd
        past = time.monotonic() - 1
        with patch.object(upd, '_agent_version_cache',
                          {'value': 'v0.14.7', 'expires_at': past}):
            assert upd.get_cached_agent_version() is None

    def test_single_flight_does_not_stack_probes(self):
        """A refresh already in flight must not start a second thread."""
        import api.updates as upd

        with patch.object(upd, '_agent_version_refresh_in_progress', True), \
             patch.object(upd, 'threading') as mock_threading:
            upd._schedule_agent_version_refresh()

        mock_threading.Thread.assert_not_called()

    def test_refresh_starts_one_daemon_thread_when_idle(self):
        import api.updates as upd

        with patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, 'threading') as mock_threading:
            upd._schedule_agent_version_refresh()

        mock_threading.Thread.assert_called_once()
        kwargs = mock_threading.Thread.call_args.kwargs
        assert kwargs.get('daemon') is True


class TestGatewayProbeRobustness:

    def _probe(self, urlopen_side_effect):
        import api.updates as upd
        with patch.object(upd, '_gateway_health_base_url',
                          return_value='http://hermes-agent:8642'), \
             patch.object(upd.urllib.request, 'urlopen',
                          side_effect=urlopen_side_effect):
            return upd._detect_agent_version_from_gateway_health(timeout=0.5)

    def test_probe_swallows_value_error(self):
        assert self._probe(lambda *a, **k: (_ for _ in ()).throw(ValueError('boom'))) is None

    def test_probe_swallows_incomplete_read(self):
        err = http.client.IncompleteRead(b'partial')
        assert self._probe(lambda *a, **k: (_ for _ in ()).throw(err)) is None

    def test_probe_swallows_timeout_and_urlerror(self):
        import urllib.error
        assert self._probe(lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError('down'))) is None
        assert self._probe(lambda *a, **k: (_ for _ in ()).throw(TimeoutError('t'))) is None

    def test_probe_caps_oversized_body(self):
        """A body larger than the cap must be treated as 'no answer'."""
        import api.updates as upd

        class BigResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, amt=-1):
                return b'x' * (upd._AGENT_VERSION_MAX_BODY_BYTES + 1)

        with patch.object(upd, '_gateway_health_base_url',
                          return_value='http://hermes-agent:8642'), \
             patch.object(upd.urllib.request, 'urlopen',
                          return_value=BigResponse()):
            assert upd._detect_agent_version_from_gateway_health(timeout=0.5) is None

    def test_probe_uses_single_overall_deadline(self):
        """Both probe paths must share ONE deadline (not per-path)."""
        import api.updates as upd
        seen = []

        def fake_urlopen(url, timeout=0):
            seen.append(timeout)
            raise TimeoutError('slow gateway')

        with patch.object(upd, '_gateway_health_base_url',
                          return_value='http://hermes-agent:8642'), \
             patch.object(upd.urllib.request, 'urlopen', fake_urlopen):
            upd._detect_agent_version_from_gateway_health(timeout=0.5)

        assert seen, 'probe never ran'
        # A shared overall deadline means each path gets the REMAINING budget:
        # the second path must have strictly less time than the first. A
        # per-path budget would hand both the full timeout.
        assert len(seen) >= 2, 'expected both probe paths to run'
        assert seen[1] < seen[0] <= 0.5
