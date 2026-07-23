"""
Tests for the dynamic version badge (issue: stale hardcoded version strings).

Covers:
  1. api/updates.py: _detect_webui_version() resolution chain
  2. api/updates.py: _detect_agent_version() detection fallback
  3. api/updates.py: WEBUI_VERSION module constant is set and non-empty
  4. api/routes.py: GET /api/settings includes webui_version and agent_version keys
  5. static/index.html: two version badges are present
  6. static/panels.js: loadSettingsPanel() populates both version badges from settings
  7. server.py: server_version is not the old hardcoded string
"""
import importlib
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. _detect_webui_version — resolution chain
# ---------------------------------------------------------------------------

class TestDetectWebUIVersion:

    def _fresh_detect(self, mock_run_git=None, version_file_content=None, scm_version=None, tmp_path=None):
        """Call _detect_webui_version() with controlled dependencies."""
        import api.updates as upd

        fake_root = tmp_path or Path('/nonexistent-path')

        if version_file_content is not None:
            vf = tmp_path / 'api' / '_version.py'
            vf.parent.mkdir(parents=True, exist_ok=True)
            vf.write_text(version_file_content, encoding='utf-8')

        scm_module = types.ModuleType('api._scm_version')
        if scm_version is not None:
            scm_module.__version__ = scm_version

        def _run_git_side_effect(args, cwd, timeout=10):
            if mock_run_git is not None:
                return mock_run_git(args, cwd, timeout)
            return ('', False)

        with patch.object(upd, '_run_git', side_effect=_run_git_side_effect), \
             patch.object(upd, 'REPO_ROOT', fake_root), \
             patch.dict(sys.modules, {'api._scm_version': scm_module}):
            return upd._detect_webui_version()

    def test_git_success_returns_tag(self, tmp_path):
        """When git describe succeeds, returns the tag string directly."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('v0.50.123', True),
            tmp_path=tmp_path,
        )
        assert result == 'v0.50.123'

    def test_git_between_tags_returns_descriptor(self, tmp_path):
        """Between releases, git describe returns a post-tag descriptor — pass it through."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('v0.50.123-3-ge91325d', True),
            tmp_path=tmp_path,
        )
        assert result == 'v0.50.123-3-ge91325d'

    def test_git_failure_falls_back_to_version_file(self, tmp_path):
        """When git fails (Docker image), falls back to api/_version.py."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            version_file_content="__version__ = 'v0.50.100'\n",
            tmp_path=tmp_path,
        )
        assert result == 'v0.50.100'

    def test_git_failure_no_version_file_returns_unknown(self, tmp_path):
        """When git fails and no _version.py exists, returns 'unknown'."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            tmp_path=tmp_path,
        )
        assert result == 'unknown'

    def test_scm_fallback_normalizes_pep440_version(self, tmp_path):
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            scm_version='0.52.2695',
            tmp_path=tmp_path,
        )
        assert result == 'v0.52.2695'

    def test_docker_version_file_precedes_scm_fallback(self, tmp_path):
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            version_file_content="__version__ = 'v0.52.100'\n",
            scm_version='0.52.2695',
            tmp_path=tmp_path,
        )
        assert result == 'v0.52.100'

    def test_malformed_scm_module_returns_unknown(self, tmp_path):
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            scm_version=None,
            tmp_path=tmp_path,
        )
        assert result == 'unknown'

    def test_version_file_malformed_returns_unknown(self, tmp_path):
        """Malformed _version.py (no recognisable __version__ assignment) returns 'unknown'."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            version_file_content="this is not valid python !!! ~~~\n",
            tmp_path=tmp_path,
        )
        assert result == 'unknown'

    def test_git_uses_fast_describe_flags(self, tmp_path):
        """git describe avoids --dirty so WSL /mnt checkouts do not stall."""
        called_args = []

        def capture(args, cwd, timeout=10):
            called_args.append(args)
            return ('v0.50.123', True)

        self._fresh_detect(mock_run_git=capture, tmp_path=tmp_path)
        assert called_args, 'git was never called'
        assert '--tags' in called_args[0]
        assert '--always' in called_args[0]
        assert '--dirty' not in called_args[0]

    def test_dirty_check_appends_suffix_when_fast(self, tmp_path):
        """A dirty worktree still gets a suffix when the cheap probe returns quickly."""
        calls = []

        def fake_run_git(args, cwd, timeout=10):
            calls.append((args, timeout))
            if args[:3] == ['describe', '--tags', '--always']:
                return ('v0.50.123', True)
            if args[:2] == ['diff-index', '--quiet']:
                return ('', False)
            return ('unexpected', False)

        result = self._fresh_detect(mock_run_git=fake_run_git, tmp_path=tmp_path)
        assert result == 'v0.50.123-dirty'
        assert calls[1][0][:2] == ['diff-index', '--quiet']

    def test_dirty_check_hashes_tracked_diff_when_available(self, tmp_path):
        """Dirty dev-build asset URLs should change on each tracked diff edit."""
        import hashlib

        diff = "diff --git a/static/ui.js b/static/ui.js\n+console.log('dev edit')\n"
        expected = hashlib.sha1(diff.encode('utf-8', errors='replace')).hexdigest()[:8]
        calls = []

        def fake_run_git(args, cwd, timeout=10):
            calls.append((args, timeout))
            if args[:3] == ['describe', '--tags', '--always']:
                return ('v0.50.123', True)
            if args[:2] == ['diff-index', '--quiet']:
                return ('git exited with status 1', False)
            if args[:3] == ['diff', '--binary', 'HEAD']:
                return (diff, True)
            return ('unexpected', False)

        result = self._fresh_detect(mock_run_git=fake_run_git, tmp_path=tmp_path)
        assert result == f'v0.50.123-dirty-{expected}'
        assert calls[2][0][:3] == ['diff', '--binary', 'HEAD']

    def test_dirty_check_falls_back_when_diff_hash_unavailable(self, tmp_path):
        """If the diff read fails, keep the old best-effort -dirty suffix."""
        def fake_run_git(args, cwd, timeout=10):
            if args[:3] == ['describe', '--tags', '--always']:
                return ('v0.50.123', True)
            if args[:2] == ['diff-index', '--quiet']:
                return ('git exited with status 1', False)
            if args[:3] == ['diff', '--binary', 'HEAD']:
                return ('git diff timed out after 1s', False)
            return ('unexpected', False)

        result = self._fresh_detect(mock_run_git=fake_run_git, tmp_path=tmp_path)
        assert result == 'v0.50.123-dirty'

    def test_dirty_suffix_changes_when_tracked_diff_changes(self, tmp_path):
        """Real git diff hashing should produce a new suffix for each tracked edit."""
        import api.updates as upd

        def git(*args):
            subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True)

        git('init')
        git('config', 'user.name', 'Test User')
        git('config', 'user.email', 'test@example.invalid')
        tracked = tmp_path / 'tracked.txt'
        tracked.write_text('base\n', encoding='utf-8')
        git('add', 'tracked.txt')
        git('commit', '-m', 'base')

        tracked.write_text('edit one\n', encoding='utf-8')
        first = upd._dirty_suffix(tmp_path)
        tracked.write_text('edit two\n', encoding='utf-8')
        second = upd._dirty_suffix(tmp_path)

        assert first.startswith('-dirty-')
        assert second.startswith('-dirty-')
        assert first != second

    def test_dirty_check_timeout_does_not_hide_base_version(self, tmp_path):
        """If dirty detection times out, keep the base version instead of unknown."""
        def fake_run_git(args, cwd, timeout=10):
            if args[:3] == ['describe', '--tags', '--always']:
                return ('v0.50.123', True)
            if args[:2] == ['diff-index', '--quiet']:
                return ('git diff-index --quiet HEAD -- timed out after 1s', False)
            return ('unexpected', False)

        result = self._fresh_detect(mock_run_git=fake_run_git, tmp_path=tmp_path)
        assert result == 'v0.50.123'


# ---------------------------------------------------------------------------
# 2. _detect_agent_version — resolution chain
# ---------------------------------------------------------------------------

class TestDetectAgentVersion:

    def _fresh_detect(self, mock_run_git=None, version_file_content=None, tmp_path=None):
        """Call _detect_agent_version() with controlled dependencies."""
        import api.updates as upd

        fake_root = tmp_path or Path('/nonexistent-agent-path')

        if version_file_content is not None:
            vf = fake_root / 'VERSION'
            vf.write_text(version_file_content, encoding='utf-8')

        def _run_git_side_effect(args, cwd, timeout=10):
            if mock_run_git is not None:
                return mock_run_git(args, cwd, timeout)
            return ('', False)

        with patch.object(upd, '_run_git', side_effect=_run_git_side_effect), \
             patch.object(upd, '_AGENT_DIR', fake_root):
            return upd._detect_agent_version()

    def test_version_file_is_preferred(self, tmp_path):
        """Agent VERSION file should be read before git fallback."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('v0.50.999', True),
            version_file_content='v0.60.1\n',
            tmp_path=tmp_path,
        )
        assert result == 'v0.60.1'

    def test_git_fallback_used_when_version_file_missing(self, tmp_path):
        """When VERSION file is absent, we fall back to git describe in agent path."""
        (tmp_path / '.git').mkdir()
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('v0.60.2', True),
            tmp_path=tmp_path,
        )
        assert result == 'v0.60.2'

    def test_missing_agent_returns_not_detected(self):
        """When no agent checkout is available, detect function returns 'not detected'."""
        import api.updates as upd
        with patch.object(upd, '_AGENT_DIR', None):
            assert upd._detect_agent_version() == 'not detected'

    def test_agent_detect_returns_not_detected_on_fail(self, tmp_path):
        """Git fallback failure should remain user-friendly and not raise."""
        result = self._fresh_detect(
            mock_run_git=lambda args, cwd, timeout: ('', False),
            tmp_path=tmp_path,
        )
        assert result == 'not detected'


# ---------------------------------------------------------------------------
# 3. WEBUI_VERSION module constant
# ---------------------------------------------------------------------------

class TestWebUIVersionConstant:

    def test_webui_version_is_set(self):
        """WEBUI_VERSION is a non-empty string exported from api.updates."""
        import api.updates as upd
        assert hasattr(upd, 'WEBUI_VERSION'), 'WEBUI_VERSION not exported from api.updates'
        assert isinstance(upd.WEBUI_VERSION, str)
        assert upd.WEBUI_VERSION, 'WEBUI_VERSION must not be empty string'

    def test_webui_version_is_not_old_hardcoded(self):
        """WEBUI_VERSION must not be the old stale value from server.py."""
        import api.updates as upd
        # These were the two stale hardcoded strings before this fix
        assert upd.WEBUI_VERSION not in ('0.50.38', 'HermesWebUI/0.50.38'), (
            'WEBUI_VERSION still holds the old hardcoded server.py value'
        )


# ---------------------------------------------------------------------------
# 4. GET /api/settings includes webui_version and agent_version
# ---------------------------------------------------------------------------

class TestSettingsEndpointVersion:

    def test_api_settings_includes_webui_version(self):
        """GET /api/settings response dict must include webui_version key."""
        import api.routes as routes
        import api.updates as upd

        # Patch load_settings to return a minimal dict (no disk I/O)
        minimal_settings = {'send_key': 'enter', 'theme': 'dark'}

        handler = MagicMock()
        from urllib.parse import urlparse
        parsed = urlparse('/api/settings')

        captured = {}

        def fake_j(h, data, status=200):
            captured['data'] = data

        with patch('api.routes.load_settings', return_value=dict(minimal_settings)), \
             patch('api.routes.j', side_effect=fake_j):
            routes.handle_get(handler, parsed)

        assert 'webui_version' in captured.get('data', {}), (
            '/api/settings response must contain webui_version key'
        )
        assert captured['data']['webui_version'] == upd.WEBUI_VERSION
        assert 'agent_version' in captured.get('data', {}), (
            '/api/settings response must contain agent_version key'
        )
        assert captured['data']['agent_version'] == upd.AGENT_VERSION

    def test_api_settings_webui_version_not_empty(self):
        """webui_version and agent_version in /api/settings must be non-empty strings."""
        import api.routes as routes

        handler = MagicMock()
        from urllib.parse import urlparse
        parsed = urlparse('/api/settings')

        captured = {}

        def fake_j(h, data, status=200):
            captured['data'] = data

        with patch('api.routes.load_settings', return_value={}), \
             patch('api.routes.j', side_effect=fake_j):
            routes.handle_get(handler, parsed)

        version = captured.get('data', {}).get('webui_version', '')
        assert version, 'webui_version in /api/settings must not be empty'
        agent_version = captured.get('data', {}).get('agent_version', '')
        assert agent_version, 'agent_version in /api/settings must not be empty'

    def test_api_settings_no_password_hash(self):
        """password_hash must still be stripped even with version injection."""
        import api.routes as routes

        handler = MagicMock()
        from urllib.parse import urlparse
        parsed = urlparse('/api/settings')

        captured = {}

        def fake_j(h, data, status=200):
            captured['data'] = data

        with patch('api.routes.load_settings', return_value={'password_hash': 'secret123'}), \
             patch('api.routes.j', side_effect=fake_j):
            routes.handle_get(handler, parsed)

        assert 'password_hash' not in captured.get('data', {}), (
            'password_hash must still be stripped from /api/settings'
        )


# ---------------------------------------------------------------------------
# 5. static/index.html — version badges
# ---------------------------------------------------------------------------

class TestIndexHTMLBadge:

    def _read_html(self):
        return (REPO_ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

    def test_old_stale_version_removed_from_html(self):
        """The old hardcoded v0.50.87 badge must not appear in index.html."""
        html = self._read_html()
        assert 'v0.50.87' not in html, (
            'Stale hardcoded version v0.50.87 still present in index.html. '
            'The badge should be a neutral placeholder; JS populates it at runtime.'
        )

    def test_badge_element_still_present(self):
        """System version badge spans must still be in the DOM for both WebUI and Agent pills."""
        html = self._read_html()
        assert 'settings-webui-version-badge' in html, (
            'WebUI badge element missing from index.html'
        )
        assert 'settings-agent-version-badge' in html, (
            'Agent badge element missing from index.html'
        )


# ---------------------------------------------------------------------------
# 6. static/panels.js — badge population from settings
# ---------------------------------------------------------------------------

class TestPanelsJSVersionBadge:

    def _read_js(self):
        return (REPO_ROOT / 'static' / 'panels.js').read_text(encoding='utf-8')

    def test_panels_js_reads_webui_version(self):
        """loadSettingsPanel must reference settings.webui_version to populate the badge."""
        src = self._read_js()
        assert 'webui_version' in src, (
            'panels.js loadSettingsPanel() must read settings.webui_version '
            'to populate the badge dynamically'
        )

    def test_panels_js_targets_version_badges(self):
        """loadSettingsPanel must target the two version badge elements."""
        src = self._read_js()
        assert 'settings-webui-version-badge' in src, (
            'panels.js must query #settings-webui-version-badge to update the WebUI text'
        )
        assert 'settings-agent-version-badge' in src, (
            'panels.js must query #settings-agent-version-badge to update the Agent text'
        )
        assert 'agent_version' in src, (
            'loadSettingsPanel must read settings.agent_version to populate the agent badge'
        )


# ---------------------------------------------------------------------------
# 7. server.py — server_version not the old hardcoded string
# ---------------------------------------------------------------------------

class TestServerVersionHeader:

    def test_server_version_not_old_hardcoded(self):
        """server.py Handler.server_version must not be the stale hardcoded value."""
        src = (REPO_ROOT / 'server.py').read_text(encoding='utf-8')
        assert 'HermesWebUI/0.50.38' not in src, (
            'server.py still contains the old hardcoded server_version string. '
            'It should use WEBUI_VERSION from api.updates.'
        )

    def test_server_version_uses_webui_version(self):
        """server.py must reference WEBUI_VERSION when setting server_version."""
        src = (REPO_ROOT / 'server.py').read_text(encoding='utf-8')
        assert 'WEBUI_VERSION' in src, (
            'server.py must import and use WEBUI_VERSION from api.updates '
            'to keep the HTTP Server: header in sync with git tags'
        )

    def test_server_py_imports_webui_version(self):
        """server.py must import WEBUI_VERSION from api.updates."""
        src = (REPO_ROOT / 'server.py').read_text(encoding='utf-8')
        assert 'from api.updates import WEBUI_VERSION' in src, (
            'server.py must import WEBUI_VERSION from api.updates'
        )

    def test_server_version_no_slash_when_unknown(self):
        """When WEBUI_VERSION is 'unknown', server_version must be bare 'HermesWebUI' with no slash."""
        src = (REPO_ROOT / 'server.py').read_text(encoding='utf-8')
        # The guard must be present so log aggregators don't see 'HermesWebUI/unknown'
        assert "'unknown'" in src or '"unknown"' in src, (
            "server.py must guard against emitting 'HermesWebUI/unknown' as the server header"
        )

    def test_server_version_uses_removeprefix_not_lstrip(self):
        """server.py must use str.removeprefix() to strip 'v', not lstrip() which strips chars."""
        src = (REPO_ROOT / 'server.py').read_text(encoding='utf-8')
        assert 'lstrip' not in src, (
            "server.py must use removeprefix('v') not lstrip('v') — lstrip strips characters, "
            "not a prefix, and would incorrectly mangle strings like 'vvv0.50.124'"
        )
        assert 'removeprefix' in src, (
            "server.py must use removeprefix('v') to strip the leading 'v' from the version tag"
        )


# ---------------------------------------------------------------------------
# 8. _cached_agent_version_from_gateway — thread-safe TTL cache
# ---------------------------------------------------------------------------

class TestCachedAgentVersionFromGateway:

    def _reset_cache(self):
        """Reset the module-level cache to empty/unset state."""
        import api.updates as upd
        upd._GATEWAY_AGENT_VERSION_CACHE["value"] = None
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = 0.0

    def test_single_flight_on_cache_miss(self):
        """Eight concurrent callers on an empty cache produce exactly one
        gateway detector call and share the same result."""
        import api.updates as upd

        self._reset_cache()

        call_count = 0
        call_lock = threading.Lock()

        def counting_detector(timeout=0.75):
            nonlocal call_count
            with call_lock:
                call_count += 1
            return "v0.60.0"

        result_box = [None] * 8
        errors = [None] * 8
        barrier = threading.Barrier(8)

        def worker(idx):
            try:
                barrier.wait()
                with patch.object(upd, '_detect_agent_version_from_gateway_health',
                                  side_effect=counting_detector):
                    result_box[idx] = upd._cached_agent_version_from_gateway()
            except Exception as e:
                errors[idx] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for err in errors:
            assert err is None, f"Worker raised: {err}"
        assert call_count == 1, (
            f"Expected exactly 1 detector call, got {call_count}"
        )
        assert all(r == "v0.60.0" for r in result_box), (
            f"All workers must share the same result: {result_box}"
        )

    def test_positive_ttl_returns_cached(self):
        """A fresh positive cache entry is returned without re-probing."""
        import api.updates as upd

        self._reset_cache()
        # Seed the cache with a fresh positive entry
        upd._GATEWAY_AGENT_VERSION_CACHE["value"] = "v0.60.0"
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = time.monotonic()

        call_count = 0

        def counting_detector(timeout=0.75):
            nonlocal call_count
            call_count += 1
            return "v0.61.0"

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=counting_detector):
            result = upd._cached_agent_version_from_gateway()

        assert result == "v0.60.0", f"Expected cached value, got {result}"
        assert call_count == 0, f"Detector should not be called: {call_count}"

    def test_negative_ttl_returns_cached_none(self):
        """A fresh negative cache entry (None) is returned without re-probing."""
        import api.updates as upd

        self._reset_cache()
        # Seed the cache with a fresh negative entry
        upd._GATEWAY_AGENT_VERSION_CACHE["value"] = None
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = time.monotonic()

        call_count = 0

        def counting_detector(timeout=0.75):
            nonlocal call_count
            call_count += 1
            return "v0.61.0"

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=counting_detector):
            result = upd._cached_agent_version_from_gateway()

        assert result is None, f"Expected None, got {result}"
        assert call_count == 0, f"Detector should not be called: {call_count}"

    def test_expired_positive_ttl_triggers_refresh(self):
        """An expired positive cache entry triggers a refresh."""
        import api.updates as upd

        self._reset_cache()
        # Seed the cache with an expired positive entry
        upd._GATEWAY_AGENT_VERSION_CACHE["value"] = "v0.60.0"
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = time.monotonic() - 31.0

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          return_value="v0.62.0"):
            result = upd._cached_agent_version_from_gateway()

        assert result == "v0.62.0", f"Expected refreshed value, got {result}"

    def test_expired_negative_ttl_triggers_refresh(self):
        """An expired negative cache entry (None) triggers a refresh."""
        import api.updates as upd

        self._reset_cache()
        # Seed the cache with an expired negative entry
        upd._GATEWAY_AGENT_VERSION_CACHE["value"] = None
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = time.monotonic() - 6.0

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          return_value="v0.63.0"):
            result = upd._cached_agent_version_from_gateway()

        assert result == "v0.63.0", f"Expected refreshed value, got {result}"

    def test_exception_does_not_stick(self):
        """A detector returning None (simulating caught exception) is cached
        with negative TTL.  After the negative TTL expires, a new caller
        re-probes and gets an updated value — the cache is not stuck."""
        import api.updates as upd

        self._reset_cache()

        call_log = []

        def two_phase_detector(timeout=0.75):
            call_log.append("called")
            if len(call_log) == 1:
                return None  # simulate internal exception → None
            return "v0.64.0"

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=two_phase_detector):
            # First call — detector returns None (caught-exception path)
            result1 = upd._cached_agent_version_from_gateway()
            assert result1 is None, "First call should return None on failure"

            # Second call within negative TTL — uses cache, no re-probe
            result2 = upd._cached_agent_version_from_gateway()
            assert result2 is None, (
                "Second call should return cached None (negative TTL)"
            )
            assert len(call_log) == 1, (
                f"Detector should be called once within negative TTL, "
                f"got {len(call_log)}"
            )

        # Force-expire the negative cache so the next call re-probes
        upd._GATEWAY_AGENT_VERSION_CACHE["at"] = time.monotonic() - 6.0

        with patch.object(upd, '_detect_agent_version_from_gateway_health',
                          side_effect=two_phase_detector):
            # Third call — negative TTL expired, re-probes
            result3 = upd._cached_agent_version_from_gateway()
            assert result3 == "v0.64.0", f"Expected refreshed value, got {result3}"
            assert len(call_log) == 2, (
                f"Detector should be called twice total (initial fail + retry "
                f"after expiry), got {len(call_log)}"
            )
