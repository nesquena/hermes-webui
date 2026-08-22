"""Regression tests for issue #7140 - cron panel Last run / Next run timezone.

Round-2 (review #7155): a fixed numeric offset is wrong for DST and
configured-UTC; the server must publish the IANA timezone name so the
browser's Intl engine resolves the correct offset PER INSTANT.

Fix:
- api/routes.py::_server_tz_info() returns ``{server_tz_name, server_tz}`` —
  IANA name resolved per-request (HERMES_TIMEZONE → config.yaml ``timezone``
  → server-local zone) plus the legacy numeric offset as fallback.
- static/sessions.js::_formatInServerTz prefers ``Intl.DateTimeFormat`` with
  ``timeZone: <IANA name>``; numeric-offset shifting stays only as fallback.
- static/panels.js renders cron Last/Next run through _formatInServerTz.
"""

import os
import pathlib
import re
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _render_cron_detail_body() -> str:
    """Return the body of the cron detail panel renderer from panels.js."""
    start = PANELS_JS.find("function _renderCronDetail")
    assert start != -1, "_renderCronDetail must exist in static/panels.js"
    end = PANELS_JS.find("\nfunction ", start + 1)
    return PANELS_JS[start:end if end != -1 else len(PANELS_JS)]


# ---------------------------------------------------------------------------
# Frontend: cron detail panel timestamps must use the server timezone
# ---------------------------------------------------------------------------

def test_cron_panel_last_next_run_use_server_tz():
    """Cron Last run / Next run must render in the server's configured
    timezone via _formatInServerTz, not bare browser-timezone toLocaleString()."""
    body = _render_cron_detail_body()
    # The server-tz formatter (defined in sessions.js, loaded before panels.js)
    # must be wired into the cron detail renderer.
    assert "_formatInServerTz" in body, (
        "cron detail panel must reference _formatInServerTz (server timezone)"
    )
    for var, field in (("nextRun", "next_run_at"), ("lastRun", "last_run_at")):
        line = next((l for l in body.splitlines() if f"const {var} =" in l), None)
        assert line is not None, f"{var} must be computed in _renderCronDetail"
        assert f"job.{field}" in line, f"{var} must format job.{field}"
        # Must go through the server-tz formatter, not a bare browser call.
        assert "toLocaleString()" not in line, (
            f"{var} ({field}) must not render via bare browser-timezone "
            "toLocaleString()"
        )


def test_formatter_prefers_iana_name_with_intl():
    """_formatInServerTz must prefer Intl.DateTimeFormat({timeZone: IANA name})
    so DST is resolved per instant and configured-UTC renders as UTC."""
    assert "Intl.DateTimeFormat" in SESSIONS_JS
    # The IANA-name branch must appear BEFORE the numeric-offset shift.
    iana_pos = SESSIONS_JS.find("timeZone: _serverTzName")
    shift_pos = SESSIONS_JS.find("offsetMin")
    assert iana_pos != -1, "formatter must use the IANA name"
    assert shift_pos != -1, "numeric-offset fallback must remain"
    assert iana_pos < shift_pos, "IANA name path must be preferred over offset shift"
    # Numeric offset shifting must remain as the fallback (not removed).
    assert "adjusted.toLocaleString(undefined, { ...options, timeZone: 'UTC' })" in SESSIONS_JS


def test_formatter_handles_utc_and_fractional_offsets():
    """Fallback path must keep correct handling of UTC and fractional offsets
    (+0530/+0345/-0330) via the numeric-offset shift."""
    assert "Etc/GMT" in SESSIONS_JS, "Etc/GMT fallback must remain"
    # Fractional offsets are handled by the shift, not Etc/GMT.
    assert "fractional" in SESSIONS_JS


# ---------------------------------------------------------------------------
# Backend: server_tz_info publishes IANA name + offset, config-aware
# ---------------------------------------------------------------------------

def test_routes_defines_server_tz_info():
    """api/routes.py must define _server_tz_info() returning both the IANA
    name and the legacy numeric offset."""
    assert "def _server_tz_info" in ROUTES_PY
    assert "server_tz_name" in ROUTES_PY
    assert '"server_tz"' in ROUTES_PY
    # Payload must spread the helper's result (both keys).
    assert "**_server_tz_info()" in ROUTES_PY


def test_server_tz_info_resolves_iana_name_from_env():
    """HERMES_TIMEZONE env var must be the highest-priority source for the
    IANA name (no hermes_time cache involved)."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "America/New_York"}, clear=False):
        info = routes._server_tz_info()
    assert info["server_tz_name"] == "America/New_York"
    assert isinstance(info["server_tz"], str) and info["server_tz"]


def test_server_tz_info_does_not_use_cached_hermes_time():
    """The helper must NOT depend on hermes_time's process-global cached tz
    (review #7155: it stays stale after a config change until reset_cache)."""
    import api.routes as routes

    helper_start = ROUTES_PY.find("def _server_tz_info")
    helper_end = ROUTES_PY.find("\ndef ", helper_start + 1)
    helper = ROUTES_PY[helper_start:helper_end]
    # Exclude the docstring (which mentions hermes_time as a negative) —
    # check only the executable body.
    body_start = helper.find('"""', helper.find('"""') + 3) + 3
    body = helper[body_start:]
    assert "hermes_time" not in body, (
        "_server_tz_info must not use the cached hermes_time resolver"
    )
    assert "import hermes_time" not in ROUTES_PY[:helper_start], (
        "hermes_time must not be imported for _server_tz_info"
    )
    assert "_load_yaml_config_file" in helper, (
        "_server_tz_info must read config via the mtime-aware reader"
    )


def test_server_tz_info_falls_back_to_server_local_zone():
    """With no env/config timezone, the helper must fall back to the
    server-local zone name (never an empty IANA name)."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {}, clear=False):
        with mock.patch.object(routes, "_load_yaml_config_file", return_value={}):
            info = routes._server_tz_info()
    # Server-local zone (e.g. "Etc/UTC" or "America/Sao_Paulo") — non-empty.
    assert isinstance(info["server_tz_name"], str)
    assert info["server_tz_name"].strip() != ""
    assert isinstance(info["server_tz"], str) and info["server_tz"]


# ---------------------------------------------------------------------------
# Behavioral: DST correctness (the core of review #7155)
# ---------------------------------------------------------------------------

def test_iana_path_behavior_dst_transition():
    """Simulate the browser path across a DST boundary: with an IANA name the
    offset for a winter instant differs from a summer instant (New York:
    Jan = UTC-5, Jul = UTC-4). Uses Node to execute the real formatter."""
    import subprocess

    script = r"""
const fs = require('fs');
const src = fs.readFileSync('static/sessions.js', 'utf8');
// Extract the two functions we need by slicing from their defs.
function grab(name) {
  const start = src.indexOf('function ' + name);
  const end = src.indexOf('\nfunction ', start + 1);
  return src.slice(start, end === -1 ? src.length : end);
}
let _serverTzName = 'America/New_York';
let _serverTz = '-0500';
const fns = grab('_formatInServerTz');
eval(fns);
const winter = new Date('2026-01-15T12:00:00Z'); // NY winter: EST (UTC-5) -> 07:00
const summer = new Date('2026-07-15T12:00:00Z'); // NY summer: EDT (UTC-4) -> 08:00
const w = _formatInServerTz(winter, {hour:'2-digit',minute:'2-digit',hour12:false});
const s = _formatInServerTz(summer, {hour:'2-digit',minute:'2-digit',hour12:false});
// Hours must DIFFER across the DST boundary (7 vs 8), proving per-instant resolution.
const wH = parseInt(w.split(':')[0], 10);
const sH = parseInt(s.split(':')[0], 10);
if (wH === sH) { console.error('DST NOT RESOLVED: winter=' + w + ' summer=' + s); process.exit(1); }
console.log('DST OK: winter=' + w + ' summer=' + s);
"""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        proc = subprocess.run(
            ["node", script_path], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30
        )
    finally:
        os.unlink(script_path)
    assert proc.returncode == 0, f"node DST check failed: {proc.stderr}\n{proc.stdout}"
    assert "DST OK" in proc.stdout


def test_iana_path_behavior_configured_utc():
    """A configured-UTC zone must render as UTC (review #7155 bug 2), not
    fall back to the browser's local zone."""
    import subprocess
    import tempfile

    script = r"""
const fs = require('fs');
const src = fs.readFileSync('static/sessions.js', 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name);
  const end = src.indexOf('\nfunction ', start + 1);
  return src.slice(start, end === -1 ? src.length : end);
}
let _serverTzName = 'UTC';
let _serverTz = '+0000';
eval(grab('_formatInServerTz'));
const out = _formatInServerTz(new Date('2026-03-10T13:00:00Z'), {hour:'2-digit',minute:'2-digit',hour12:false});
const h = parseInt(out.split(':')[0], 10);
if (h !== 13) { console.error('CONFIGURED UTC WRONG: got ' + out); process.exit(1); }
console.log('UTC OK: ' + out);
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        proc = subprocess.run(
            ["node", script_path], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30
        )
    finally:
        os.unlink(script_path)
    assert proc.returncode == 0, f"node UTC check failed: {proc.stderr}\n{proc.stdout}"
    assert "UTC OK" in proc.stdout


if __name__ == "__main__":
    unittest.main()
