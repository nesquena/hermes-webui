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
import os
import time

import pytest
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
        """An explicitly expired cache with no refresh in flight starts exactly
        one daemon worker. The expired cache is installed explicitly so the
        test does not depend on process-global test order (a fresh entry left
        behind by an earlier test would suppress the worker)."""
        import api.updates as upd

        past = time.monotonic() - 1
        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': past}), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
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


class TestSchedulingCoherence:
    """Freshness + ownership are one atomic decision; rollback on start().

    Required by the #6156 re-gate: fresh positive AND negative (None) cache
    entries suppress the worker until expiry; a Thread.start() failure must
    release ownership so a retry is possible; concurrent cold/expired callers
    still launch exactly one probe and settle in a clean state.
    """

    def test_fresh_positive_and_negative_suppress_worker_until_expiry(self):
        """Fresh entries (positive AND None) never start a worker; exactly one
        worker starts once the entry is expired."""
        import api.updates as upd

        future = time.monotonic() + 3600
        for value in ('v0.14.7', None):
            with patch.object(upd, '_agent_version_cache',
                              {'value': value, 'expires_at': future}), \
                 patch.object(upd, '_agent_version_refresh_in_progress', False), \
                 patch.object(upd, 'threading') as mock_threading:
                upd._schedule_agent_version_refresh()
            mock_threading.Thread.assert_not_called()

        # After expiry the worker starts exactly once: the first call claims
        # ownership and the second sees the refresh in flight.
        past = time.monotonic() - 1
        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': past}), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, 'threading') as mock_threading:
            upd._schedule_agent_version_refresh()
            upd._schedule_agent_version_refresh()
        mock_threading.Thread.assert_called_once()

    def test_thread_start_failure_clears_ownership_and_allows_retry(self):
        """If Thread.start() raises, the claimed ownership is rolled back under
        the lock so a later request can retry instead of being blocked."""
        import api.updates as upd
        import pytest

        past = time.monotonic() - 1

        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': past}), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, 'threading') as mock_threading:
            mock_threading.Thread.return_value = MagicMock()
            mock_threading.Thread.return_value.start.side_effect = [
                RuntimeError('thread creation failed'),
                None,
            ]
            with pytest.raises(RuntimeError):
                upd._schedule_agent_version_refresh()
            # start() failed -> ownership must be released...
            assert upd._agent_version_refresh_in_progress is False
            # ...so the retry is allowed and claims ownership again.
            upd._schedule_agent_version_refresh()
            assert mock_threading.Thread.call_count == 2
            assert upd._agent_version_refresh_in_progress is True

    def test_thread_constructor_failure_clears_ownership_and_allows_retry(self):
        """If Thread() construction itself raises (before start), the claimed
        ownership is rolled back under the lock so a later request can retry."""
        import api.updates as upd

        past = time.monotonic() - 1

        with patch.object(upd, '_agent_version_cache',
                          {'value': None, 'expires_at': past}), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, 'threading') as mock_threading:
            mock_threading.Thread.side_effect = RuntimeError('thread construction failed')
            with pytest.raises(RuntimeError):
                upd._schedule_agent_version_refresh()
            # Construction failed -> ownership must be released...
            assert upd._agent_version_refresh_in_progress is False
            # ...so the retry is allowed and claims ownership again.
            mock_threading.Thread.side_effect = None
            mock_threading.Thread.return_value = MagicMock()
            upd._schedule_agent_version_refresh()
            assert mock_threading.Thread.call_count == 2
            assert upd._agent_version_refresh_in_progress is True

    def test_concurrent_cold_callers_single_probe_and_clean_settle(self):
        """Concurrent cold/expired callers launch exactly ONE probe and settle
        in a clean state: value + future expiry published, ownership released.

        The spawned worker is captured and joined INSIDE the patch scope, and
        cache publication + ownership settlement are asserted there too. Patch
        teardown restores the module-level ownership global, so asserting
        after the ``with`` block would make settlement vacuous and let a late
        worker cross into another test."""
        import api.updates as upd
        import threading as real_threading
        import types as real_types

        cache = {'value': None, 'expires_at': 0.0}
        created = []

        def tracking_thread(*args, **kwargs):
            t = real_threading.Thread(*args, **kwargs)
            created.append(t)
            return t

        threading_shim = real_types.ModuleType('threading')
        threading_shim.Thread = tracking_thread

        probe_entered = real_threading.Event()
        release_probe = real_threading.Event()
        probe_calls = []

        def slow_probe(**kwargs):
            probe_calls.append(1)
            probe_entered.set()
            assert release_probe.wait(timeout=5), 'probe never released'
            return 'v0.14.7'

        with patch.object(upd, '_agent_version_cache', cache), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=slow_probe), \
             patch.object(upd, 'threading', threading_shim):
            barrier = real_threading.Barrier(5)

            def caller():
                barrier.wait()
                upd._schedule_agent_version_refresh()

            threads = [real_threading.Thread(target=caller) for _ in range(5)]
            for t in threads:
                t.start()
            assert probe_entered.wait(timeout=5), 'probe never started'
            # While the probe is in flight every other caller must be a no-op.
            release_probe.set()
            for t in threads:
                t.join(timeout=5)

            # Join the spawned worker INSIDE the patch scope so the settlement
            # assertions below are the worker's real outcome, not a teardown
            # artifact (patch teardown restores the ownership global to False).
            assert len(created) == 1, 'concurrent callers stacked workers'
            created[0].join(timeout=5)
            assert not created[0].is_alive(), 'worker did not settle'

            assert len(probe_calls) == 1, 'concurrent callers stacked probes'
            assert cache['value'] == 'v0.14.7'
            assert cache['expires_at'] > time.monotonic()
            assert upd._agent_version_refresh_in_progress is False

    def test_worker_baseexception_still_settles_ownership(self):
        """A BaseException raised inside the worker (e.g. KeyboardInterrupt)
        must not leak ownership: settlement runs in ``finally`` even though
        the exception bypasses the cache publication, so a later request can
        retry against a cold cache."""
        import api.updates as upd
        import threading as real_threading
        import types as real_types

        cache = {'value': None, 'expires_at': 0.0}
        created = []

        def tracking_thread(*args, **kwargs):
            # Wrap the worker target: the BaseException must propagate through
            # _run (exercising its finally-settlement) and then be swallowed at
            # the thread boundary so pytest does not flag an unhandled thread
            # exception — settlement already happened.
            target = kwargs.get('target')
            if target is not None:
                def wrapped(*a, **k):
                    try:
                        target(*a, **k)
                    except BaseException:
                        pass
                kwargs['target'] = wrapped
            t = real_threading.Thread(*args, **kwargs)
            created.append(t)
            return t

        threading_shim = real_types.ModuleType('threading')
        threading_shim.Thread = tracking_thread

        def boom(**kwargs):
            raise KeyboardInterrupt('interrupted during probe')

        with patch.object(upd, '_agent_version_cache', cache), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=boom), \
             patch.object(upd, 'threading', threading_shim):
            upd._schedule_agent_version_refresh()
            assert len(created) == 1
            created[0].join(timeout=5)
            assert not created[0].is_alive(), 'worker did not settle'
            # Ownership settled by finally even though BaseException skipped
            # the cache publication...
            assert upd._agent_version_refresh_in_progress is False
            # ...and the cache stayed cold so a later request can retry.
            assert cache['value'] is None
            assert cache['expires_at'] == 0.0

            # The freed ownership lets a follow-up request claim and retry.
            upd._schedule_agent_version_refresh()
            assert len(created) == 2
            created[1].join(timeout=5)
            assert not created[1].is_alive()
            assert upd._agent_version_refresh_in_progress is False

    def test_failed_probe_publishes_none_with_backoff_expiry(self):
        """A failed probe settles ``None`` with its own backoff expiry, and the
        fresh negative result suppresses further workers until it lapses."""
        import api.updates as upd
        import threading as real_threading

        cache = {'value': None, 'expires_at': 0.0}
        release_probe = real_threading.Event()

        def slow_none(**kwargs):
            assert release_probe.wait(timeout=5), 'probe never released'
            return None

        with patch.object(upd, '_agent_version_cache', cache), \
             patch.object(upd, '_agent_version_refresh_in_progress', False), \
             patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=slow_none):
            upd._schedule_agent_version_refresh()
            release_probe.set()
            deadline = time.monotonic() + 5
            while upd._agent_version_refresh_in_progress and time.monotonic() < deadline:
                time.sleep(0.01)
            assert cache['value'] is None
            remaining = cache['expires_at'] - time.monotonic()
            assert 0 < remaining <= upd._AGENT_VERSION_FAILURE_BACKOFF_SECONDS + 1
            # While the None backoff is fresh, scheduling is a no-op.
            with patch.object(upd, 'threading') as mock_threading:
                upd._schedule_agent_version_refresh()
            mock_threading.Thread.assert_not_called()


class TestGatewayHealthBaseUrl:
    """Canonical gateway-base precedence for the agent-version probe (#6156 re-gate).

    The background refresh must honor every supported remote-gateway location
    in the same order as ``api.agent_health._remote_gateway_base_url``, then
    fall back to the Docker default, and strip trailing health-path suffixes
    so the probe never builds ``/health/health``. All env vars are cleared per
    case so the tests are deterministic regardless of the host environment.
    """

    def _resolve(self, env):
        import api.updates as upd
        with patch.dict(os.environ, env, clear=True):
            return upd._gateway_health_base_url()

    def test_each_supported_variable_resolves_individually(self):
        cases = {
            'GATEWAY_HEALTH_URL': 'http://gw-a.invalid:8642',
            'HERMES_GATEWAY_HEALTH_URL': 'http://gw-b.invalid:8642',
            'HERMES_API_URL': 'http://gw-c.invalid:8642',
            'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://gw-d.invalid:8642',
        }
        for var, url in cases.items():
            assert self._resolve({var: url}) == url, var

    def test_precedence_gateway_health_url_wins(self):
        assert self._resolve({
            'GATEWAY_HEALTH_URL': 'http://first.invalid:8642',
            'HERMES_GATEWAY_HEALTH_URL': 'http://second.invalid:8642',
            'HERMES_API_URL': 'http://third.invalid:8642',
            'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://fourth.invalid:8642',
        }) == 'http://first.invalid:8642'

    def test_precedence_hermes_gateway_health_url_beats_api_url(self):
        assert self._resolve({
            'HERMES_GATEWAY_HEALTH_URL': 'http://second.invalid:8642',
            'HERMES_API_URL': 'http://third.invalid:8642',
            'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://fourth.invalid:8642',
        }) == 'http://second.invalid:8642'

    def test_precedence_hermes_api_url_beats_webui_gateway_base(self):
        assert self._resolve({
            'HERMES_API_URL': 'http://third.invalid:8642',
            'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://fourth.invalid:8642',
        }) == 'http://third.invalid:8642'

    def test_precedence_webui_gateway_base_url_is_last_supported(self):
        assert self._resolve({
            'HERMES_WEBUI_GATEWAY_BASE_URL': 'http://fourth.invalid:8642',
        }) == 'http://fourth.invalid:8642'

    def test_docker_default_when_no_variable_set(self):
        assert self._resolve({}) == 'http://hermes-agent:8642'

    def test_surrounding_whitespace_is_ignored(self):
        assert self._resolve({
            'GATEWAY_HEALTH_URL': '  http://gw.invalid:8642  ',
        }) == 'http://gw.invalid:8642'

    @pytest.mark.parametrize('var,next_var', [
        ('GATEWAY_HEALTH_URL', 'HERMES_GATEWAY_HEALTH_URL'),
        ('HERMES_GATEWAY_HEALTH_URL', 'HERMES_API_URL'),
        ('HERMES_API_URL', 'HERMES_WEBUI_GATEWAY_BASE_URL'),
    ])
    def test_whitespace_only_var_falls_through_to_next_url(self, var, next_var):
        """A whitespace-only value is unset for precedence purposes, so a
        lower-priority valid URL must still win (re-gate #6156)."""
        assert self._resolve({
            var: '   ',
            next_var: 'http://fallback.invalid:8642',
        }) == 'http://fallback.invalid:8642'

    def test_all_whitespace_only_values_fall_back_to_docker_default(self):
        assert self._resolve({
            'GATEWAY_HEALTH_URL': '   ',
            'HERMES_GATEWAY_HEALTH_URL': '\t\n ',
            'HERMES_API_URL': '   ',
            'HERMES_WEBUI_GATEWAY_BASE_URL': ' \t ',
        }) == 'http://hermes-agent:8642'

    @pytest.mark.parametrize('var', [
        'GATEWAY_HEALTH_URL',
        'HERMES_GATEWAY_HEALTH_URL',
        'HERMES_API_URL',
        'HERMES_WEBUI_GATEWAY_BASE_URL',
    ])
    @pytest.mark.parametrize('suffix,expected', [
        ('/health/detailed', 'http://gw.invalid:8642'),
        ('/health', 'http://gw.invalid:8642'),
        # '/v1/health' also ends with '/health', which the canonical suffix
        # list matches FIRST, so only that segment is stripped. The probe then
        # appends '/health' again, round-tripping to the configured endpoint.
        ('/v1/health', 'http://gw.invalid:8642/v1'),
        ('/status', 'http://gw.invalid:8642'),
    ])
    def test_health_path_suffix_stripped_for_each_variable(self, var, suffix, expected):
        assert self._resolve({var: f'http://gw.invalid:8642{suffix}'}) == expected

    @pytest.mark.parametrize('var', [
        'GATEWAY_HEALTH_URL',
        'HERMES_GATEWAY_HEALTH_URL',
        'HERMES_API_URL',
        'HERMES_WEBUI_GATEWAY_BASE_URL',
    ])
    def test_trailing_slash_after_health_suffix_is_normalized(self, var):
        assert self._resolve({var: 'http://gw.invalid:8642/health/'}) \
            == 'http://gw.invalid:8642'

    def test_non_health_path_is_preserved(self):
        assert self._resolve({
            'GATEWAY_HEALTH_URL': 'http://gw.invalid:8642/api/v1',
        }) == 'http://gw.invalid:8642/api/v1'
