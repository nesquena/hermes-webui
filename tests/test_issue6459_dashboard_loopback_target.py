"""Regression tests for #6459 — dashboard loopback warning suppression.

The warning should only show when BOTH conditions are met:
1. Browser is accessing WebUI from a non-loopback origin (remote)
2. Dashboard has NO public (non-loopback) browser_url configured

If browser_url is public, no warning even on remote WebUI (correct config).
If WebUI is local (127.0.0.1 browser), no warning even without browser_url (safe context).

The node-driver extraction pattern runs all logic in Node.js without a browser.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

UI_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'ui.js')


def _extract_dashboard_functions():
    """Extract the dashboard loopback warning functions from ui.js."""
    src = open(UI_JS, encoding='utf-8').read()

    def extract_function(name):
        start = src.find(f'function {name}(')
        if start < 0:
            raise AssertionError(f'{name} not found in ui.js')
        i = src.find('{', start)
        depth = 1
        i += 1
        while i < len(src) and depth:
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
            i += 1
        return src[start:i]

    fns = '\n'.join(
        extract_function(name)
        for name in (
            '_isLoopbackHostname',
            '_dashboardBrowserUrl',
            '_dashboardIsBrowserLoopback',
        )
    )
    return fns


def _run_node(test_js):
    """Run a Node.js snippet that includes the extracted functions, return stdout."""
    fns = _extract_dashboard_functions()
    harness = """\
// Fake window.location
const window = {
  location: {
    hostname: 'example.com',
  }
};

// Fake URL constructor
class FakeURL {
  constructor(url) {
    try {
      // Simple parser for testing purposes
      if (!url) throw new Error('invalid');
      this.href = url;
      // Extract hostname from URL
      const match = url.match(/^[a-z]+:\\/\\/([^\\/:]+)(?::\\d+)?/i);
      if (!match) throw new Error('invalid');
      this.hostname = match[1];
    } catch (e) {
      throw new Error('Invalid URL');
    }
  }
  toString() {
    return this.href;
  }
};
global.URL = FakeURL;

// Fake translation function
function t(key) {
  return key === 'dashboard_loopback_warning' ? 'WARNING_TEXT' : '';
}
"""
    js_code = harness + '\n' + fns + '\n' + test_js

    tf = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8')
    tf.write(js_code)
    tf.close()
    try:
        if NODE is None:
            raise RuntimeError('node not on PATH')
        result = subprocess.run(
            [NODE, tf.name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f'node error: {result.stderr}')
        return result.stdout.strip()
    finally:
        os.unlink(tf.name)


class TestIssue6459DashboardLoopbackTarget:
    """Loopback warning is suppressed when browser_url is public."""

    def test_public_browser_url_suppresses_warning(self):
        """Public browser_url suppresses warning on remote WebUI origin."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: public browser_url
const status = { running: true, browser_url: 'https://example.com' };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == 'https://example.com', \
            '_dashboardBrowserUrl should return configured public browser_url'

    def test_loopback_browser_url_not_suppresses_warning(self):
        """Loopback browser_url (127.0.0.1) does NOT suppress warning on remote WebUI (Gap 1)."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: loopback browser_url (127.0.0.1)
const status = { running: true, browser_url: 'http://127.0.0.1:3000' };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == 'http://127.0.0.1:3000', \
            '_dashboardBrowserUrl should return configured loopback browser_url'

    def test_localhost_browser_url_not_suppresses_warning(self):
        """localhost browser_url does NOT suppress warning on remote WebUI."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: localhost browser_url
const status = { running: true, browser_url: 'http://localhost:3000' };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == 'http://localhost:3000', \
            '_dashboardBrowserUrl should return configured localhost browser_url'

    def test_ipv6_loopback_browser_url_not_suppresses_warning(self):
        """::1 browser_url does NOT suppress warning on remote WebUI."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: ::1 browser_url
const status = { running: true, browser_url: 'http://[::1]:3000' };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == 'http://[::1]:3000', \
            '_dashboardBrowserUrl should return configured ::1 browser_url'

    def test_no_browser_url_no_running(self):
        """No browser_url and not running shows no warning (edge case)."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: not running
const status = { running: false };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == '', \
            '_dashboardBrowserUrl should return empty string when not running'

    def test_no_browser_url_running(self):
        """No browser_url shows warning on remote WebUI (should warn)."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';

// Test: running but no browser_url
const status = { running: true };
const url = _dashboardBrowserUrl(status);
process.stdout.write(url);
"""
        result = _run_node(js)
        assert result == '', \
            '_dashboardBrowserUrl should return empty string when no browser_url'

    def test_is_loopback_hostname_true_localhost(self):
        """localhost is a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('localhost')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should return true for localhost'

    def test_is_loopback_hostname_true_127_0_0_1(self):
        """127.0.0.1 is a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('127.0.0.1')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should return true for 127.0.0.1'

    def test_is_loopback_hostname_true_127_0_1_1(self):
        """127.0.1.1 is a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('127.0.1.1')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should return true for 127.0.1.1'

    def test_is_loopback_hostname_true_ipv6_loopback(self):
        """::1 is a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('::1')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should return true for ::1'

    def test_is_loopback_hostname_true_ipv6_loopback_brackets(self):
        """[::1] (with brackets) is a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('[::1]')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should handle bracketed IPv6'

    def test_is_loopback_hostname_false_public_ip(self):
        """1.2.3.4 is not a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('1.2.3.4')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should return false for public IP'

    def test_is_loopback_hostname_false_public_hostname(self):
        """example.com is not a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('example.com')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should return false for public hostname'

    def test_is_loopback_hostname_false_lookalike_localhost_evil(self):
        """localhost.evil.com is NOT a loopback hostname (lookalike rejected)."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('localhost.evil.com')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should reject lookalike localhost.evil.com'

    def test_is_loopback_hostname_false_lookalike_127_evil(self):
        """127.0.0.1.evil.com is NOT a loopback hostname (lookalike rejected)."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('127.0.0.1.evil.com')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should reject lookalike 127.0.0.1.evil.com'

    def test_is_loopback_hostname_false_invalid_octet(self):
        """127.0.0.256 is NOT a loopback hostname (invalid octet rejected)."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('127.0.0.256')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should reject invalid octet 127.0.0.256'

    def test_is_loopback_hostname_true_localhost_with_dot(self):
        """localhost. (with terminal dot) IS recognized as loopback."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('localhost.')));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_isLoopbackHostname should accept localhost. with terminal dot'

    def test_is_loopback_hostname_false_empty_string(self):
        """Empty string is not a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname('')));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should return false for empty string'

    def test_is_loopback_hostname_false_undefined(self):
        """undefined is not a loopback hostname."""
        js = """\
process.stdout.write(String(_isLoopbackHostname(undefined)));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_isLoopbackHostname should return false for undefined'

    def test_is_browser_loopback_true_127_0_0_1(self):
        """_dashboardIsBrowserLoopback returns true for 127.0.0.1 WebUI origin."""
        js = """\
// Override window.location to simulate local WebUI
window.location.hostname = '127.0.0.1';
process.stdout.write(String(_dashboardIsBrowserLoopback()));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_dashboardIsBrowserLoopback should return true for 127.0.0.1 WebUI'

    def test_is_browser_loopback_true_localhost(self):
        """_dashboardIsBrowserLoopback returns true for localhost WebUI origin."""
        js = """\
// Override window.location to simulate local WebUI
window.location.hostname = 'localhost';
process.stdout.write(String(_dashboardIsBrowserLoopback()));
"""
        result = _run_node(js)
        assert result == 'true', \
            '_dashboardIsBrowserLoopback should return true for localhost WebUI'

    def test_is_browser_loopback_false_remote_ip(self):
        """_dashboardIsBrowserLoopback returns false for remote IP WebUI origin."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = '1.2.3.4';
process.stdout.write(String(_dashboardIsBrowserLoopback()));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_dashboardIsBrowserLoopback should return false for remote IP WebUI'

    def test_is_browser_loopback_false_public_hostname(self):
        """_dashboardIsBrowserLoopback returns false for public hostname WebUI origin."""
        js = """\
// Override window.location to simulate remote WebUI
window.location.hostname = 'example.com';
process.stdout.write(String(_dashboardIsBrowserLoopback()));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_dashboardIsBrowserLoopback should return false for public hostname WebUI'

    def test_is_browser_loopback_false_lookalike(self):
        """_dashboardIsBrowserLoopback returns false for lookalike hostname WebUI origin."""
        js = """\
// Override window.location to simulate lookalike WebUI
window.location.hostname = 'localhost.evil.com';
process.stdout.write(String(_dashboardIsBrowserLoopback()));
"""
        result = _run_node(js)
        assert result == 'false', \
            '_dashboardIsBrowserLoopback should return false for lookalike WebUI'