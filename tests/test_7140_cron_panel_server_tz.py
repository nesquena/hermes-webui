"""Regression tests for issue #7140 - cron panel Last run / Next run timezone.

Round-2 (review #7155): a fixed numeric offset is wrong for DST and
configured-UTC; the server must publish the IANA timezone name so the
browser's Intl engine resolves the correct offset PER INSTANT.

Fix:
- api/routes.py::_server_tz_info() returns ``{server_tz_name, server_tz}`` —
  IANA name resolved per-request (HERMES_TIMEZONE → config.yaml ``timezone``
  → server-local zone) plus the legacy numeric offset as fallback. The config
  read goes through the mtime-aware ``_load_yaml_config_file``, so a runtime
  config change is picked up on the next request — no process-global
  ``hermes_time`` cache (which stays stale until reset_cache).
- static/sessions.js::_formatInServerTz prefers ``Intl.DateTimeFormat`` with
  ``timeZone: <IANA name>``; numeric-offset shifting stays only as fallback.
  An offset-only payload of ``+0000`` (explicit server UTC) renders as UTC,
  not the browser's local zone.
- static/panels.js renders cron Last/Next run through _formatInServerTz.

Tests are behavioral: the shipped JS formatter is executed with Node against
real instants (UTC, fractional offsets, both sides of DST transitions) and
the Python resolver is exercised with mocked config/env changes at runtime.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()

# Node preamble: load the REAL _formatInServerTz implementation out of
# static/sessions.js so the tests exercise the shipped code, not a copy.
_GRAB_FORMATTER = r"""
const fs = require('fs');
const src = fs.readFileSync('static/sessions.js', 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name);
  const end = src.indexOf('\nfunction ', start + 1);
  return src.slice(start, end === -1 ? src.length : end);
}
eval(grab('_formatInServerTz'));
"""


def _node(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a Node snippet in the repo root and return the completed process."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        return subprocess.run(
            ["node", script_path],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        os.unlink(script_path)


def _assert_node_ok(proc: subprocess.CompletedProcess, label: str):
    assert proc.returncode == 0, f"node {label} failed: {proc.stderr}\n{proc.stdout}"


# ---------------------------------------------------------------------------
# Frontend behavior (Node executes the real formatter): IANA name path
# ---------------------------------------------------------------------------

def test_formatter_iana_configured_utc_renders_utc():
    """A configured-UTC zone (IANA name 'UTC') must render as UTC, not the
    browser's local zone (review #7155 bug 2)."""
    proc = _node(_GRAB_FORMATTER + r"""
let _serverTzName = 'UTC';
let _serverTz = '+0000';
const out = _formatInServerTz(new Date('2026-03-10T13:00:00Z'), {hour:'2-digit',minute:'2-digit',hour12:false});
const h = parseInt(out.split(':')[0], 10);
if (h !== 13) { console.error('CONFIGURED UTC WRONG: got ' + out); process.exit(1); }
console.log('UTC OK: ' + out);
""")
    _assert_node_ok(proc, "configured-UTC")
    assert "UTC OK" in proc.stdout


def test_formatter_iana_dst_spring_forward_both_sides():
    """America/New_York 2026-03-08 spring forward: 06:59:59Z is EST (01:59:59)
    and 07:00:00Z is EDT (03:00:00) — the offset must flip per instant."""
    proc = _node(_GRAB_FORMATTER + r"""
let _serverTzName = 'America/New_York';
let _serverTz = '-0500';
const opts = {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false};
const before = _formatInServerTz(new Date('2026-03-08T06:59:59Z'), opts); // EST UTC-5
const after  = _formatInServerTz(new Date('2026-03-08T07:00:00Z'), opts); // EDT UTC-4
if (before !== '01:59:59') { console.error('SPRING BEFORE WRONG: ' + before); process.exit(1); }
if (after !== '03:00:00') { console.error('SPRING AFTER WRONG: ' + after); process.exit(1); }
console.log('SPRING OK: ' + before + ' -> ' + after);
""")
    _assert_node_ok(proc, "DST spring-forward")
    assert "SPRING OK" in proc.stdout


def test_formatter_iana_dst_fall_back_both_sides():
    """America/New_York 2026-11-01 fall back: 05:59:59Z is EDT (01:59:59) and
    06:00:00Z is EST (01:00:00) — the offset must flip per instant."""
    proc = _node(_GRAB_FORMATTER + r"""
let _serverTzName = 'America/New_York';
let _serverTz = '-0400';
const opts = {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false};
const before = _formatInServerTz(new Date('2026-11-01T05:59:59Z'), opts); // EDT UTC-4
const after  = _formatInServerTz(new Date('2026-11-01T06:00:00Z'), opts); // EST UTC-5
if (before !== '01:59:59') { console.error('FALL BEFORE WRONG: ' + before); process.exit(1); }
if (after !== '01:00:00') { console.error('FALL AFTER WRONG: ' + after); process.exit(1); }
console.log('FALL OK: ' + before + ' -> ' + after);
""")
    _assert_node_ok(proc, "DST fall-back")
    assert "FALL OK" in proc.stdout


# ---------------------------------------------------------------------------
# Frontend behavior: numeric-offset fallback path (no IANA name published)
# ---------------------------------------------------------------------------

def test_formatter_numeric_fractional_offsets():
    """Offset-only payloads (no IANA name) with fractional-hour offsets
    +0530/+0345/-0330 must render the correct wall clock via the numeric
    shift fallback (review #7155: Etc/GMT cannot express them)."""
    cases = {
        "+0530": "18:30",  # Asia/Kolkata
        "+0345": "16:45",  # Asia/Kathmandu
        "-0330": "09:30",  # Canada/Newfoundland
    }
    script = _GRAB_FORMATTER + r"""
let _serverTzName = '';
let _serverTz = process.env.TZ_OFFSET;
const out = _formatInServerTz(new Date('2026-03-10T13:00:00Z'), {hour:'2-digit',minute:'2-digit',hour12:false});
const expected = process.env.TZ_EXPECTED;
if (out !== expected) {
  console.error('OFFSET ' + _serverTz + ' WRONG: got ' + out + ' expected ' + expected);
  process.exit(1);
}
console.log('OFFSET ' + _serverTz + ' OK: ' + out);
"""
    for tz, expected in cases.items():
        env = dict(os.environ, TZ_OFFSET=tz, TZ_EXPECTED=expected)
        proc = _node(script, env=env)
        _assert_node_ok(proc, f"fractional offset {tz}")
        assert f"OFFSET {tz} OK" in proc.stdout


def test_formatter_offset_only_utc_renders_utc():
    """Review #7155 bug 3: an offset-only payload of '+0000'/'-0000' (server
    UTC, no IANA name) must render as UTC, NOT the browser's local zone. The
    node process runs with TZ=America/New_York to prove the browser-local
    fallback is not taken (13:00Z would read 08:00 there)."""
    script = _GRAB_FORMATTER + r"""
let _serverTzName = '';
let _serverTz = process.env.TZ_OFFSET;
const out = _formatInServerTz(new Date('2026-03-10T13:00:00Z'), {hour:'2-digit',minute:'2-digit',hour12:false});
if (out !== '13:00') {
  console.error('OFFSET-UTC WRONG (' + _serverTz + '): got ' + out + ' — browser-local fallback taken?');
  process.exit(1);
}
console.log('OFFSET-UTC OK (' + _serverTz + '): ' + out);
"""
    for tz in ("+0000", "-0000"):
        env = dict(os.environ, TZ="America/New_York", TZ_OFFSET=tz)
        proc = _node(script, env=env)
        _assert_node_ok(proc, f"offset-only UTC {tz}")
        assert f"OFFSET-UTC OK ({tz})" in proc.stdout


def test_panel_null_timestamps_and_wiring():
    """The cron detail panel must render t('not_available') / t('never') for
    null timestamps and route non-null timestamps through the server-tz
    formatter. Executes the actual nextRun/lastRun computation lines from
    static/panels.js with a stubbed formatter."""
    script = r"""
const fs = require('fs');
const panels = fs.readFileSync('static/panels.js', 'utf8');
const start = panels.indexOf('function _renderCronDetail(job){');
const end = panels.indexOf('\nfunction ', start + 1);
const body = panels.slice(start, end === -1 ? panels.length : end);
const lines = body.split('\n').filter(l =>
  /const (_fmtCronTz|_fmtCronTs|nextRun|lastRun) =/.test(l)
).join('\n');
const run = new Function('job', 't', '_formatInServerTz',
  lines + '\nreturn { nextRun, lastRun };');
const t = (k) => 'T(' + k + ')';
const fmt = (d, o) => 'FMT:' + d.toISOString();
// Null timestamps -> localized placeholders, no formatter call.
let r = run({ next_run_at: null, last_run_at: null }, t, fmt);
if (r.nextRun !== 'T(not_available)' || r.lastRun !== 'T(never)') {
  console.error('NULL WRONG: ' + JSON.stringify(r)); process.exit(1);
}
// Non-null timestamps flow through the server-tz formatter as Dates.
r = run({ next_run_at: '2026-03-10T13:00:00Z', last_run_at: '2026-03-09T22:00:00Z' }, t, fmt);
if (r.nextRun !== 'FMT:2026-03-10T13:00:00.000Z' || r.lastRun !== 'FMT:2026-03-09T22:00:00.000Z') {
  console.error('NON-NULL WRONG: ' + JSON.stringify(r)); process.exit(1);
}
console.log('PANEL OK');
"""
    proc = _node(script)
    _assert_node_ok(proc, "panel null timestamps")
    assert "PANEL OK" in proc.stdout


# ---------------------------------------------------------------------------
# Backend: server_tz_info publishes IANA name + offset, resolved per request
# ---------------------------------------------------------------------------

def test_server_tz_info_resolves_iana_name_from_env():
    """HERMES_TIMEZONE env var must be the highest-priority source for the
    IANA name (no hermes_time cache involved)."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "America/New_York"}, clear=False):
        info = routes._server_tz_info()
    assert info["server_tz_name"] == "America/New_York"
    assert isinstance(info["server_tz"], str) and info["server_tz"]


def test_server_tz_info_resolves_iana_name_from_config():
    """Without the env var, the timezone key in config.yaml (read via the
    mtime-aware _load_yaml_config_file) must supply the IANA name."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": ""}, clear=False):
        with mock.patch.object(routes, "_load_yaml_config_file", return_value={"timezone": "Asia/Kolkata"}):
            info = routes._server_tz_info()
    assert info["server_tz_name"] == "Asia/Kolkata"


def test_server_tz_info_picks_up_runtime_config_change():
    """Review #7155 bug: the Agent's process-global hermes_time cache stays
    stale after a config change. The WebUI resolver must read config per
    request: two calls with different config.yaml contents must return
    different IANA names (proves mtime-aware, non-cached resolution)."""
    import api.routes as routes

    configs = iter([{"timezone": "America/New_York"}, {"timezone": "Asia/Kolkata"}])
    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": ""}, clear=False):
        with mock.patch.object(
            routes, "_load_yaml_config_file", side_effect=lambda _path: next(configs)
        ):
            first = routes._server_tz_info()
            second = routes._server_tz_info()
    assert first["server_tz_name"] == "America/New_York"
    assert second["server_tz_name"] == "Asia/Kolkata"
    assert first != second


def test_server_tz_info_picks_up_runtime_env_change():
    """A HERMES_TIMEZONE change between requests must be reflected on the next
    response (per-request resolution, not a cached value)."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "America/New_York"}, clear=False):
        first = routes._server_tz_info()
    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "Europe/London"}, clear=False):
        second = routes._server_tz_info()
    assert first["server_tz_name"] == "America/New_York"
    assert second["server_tz_name"] == "Europe/London"


def test_session_list_payload_includes_server_tz_name_and_offset():
    """The /api/sessions response payload must carry both server_tz_name and
    server_tz, with the per-request resolved IANA name."""
    import api.routes as routes

    with mock.patch.object(routes, "_session_list_cache_overlay_runtime_rows", return_value=[]):
        with mock.patch.object(routes, "load_settings", return_value={}):
            with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "Asia/Kolkata"}, clear=False):
                resp = routes._session_list_payload_to_response({})
    assert resp["server_tz_name"] == "Asia/Kolkata"
    assert isinstance(resp["server_tz"], str) and resp["server_tz"]


def test_server_tz_info_falls_back_to_server_local_zone():
    """With no env/config timezone, the helper must fall back to a non-empty
    server-local IANA name (e.g. 'UTC' or the process zone)."""
    import api.routes as routes

    with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": ""}, clear=False):
        with mock.patch.object(routes, "_load_yaml_config_file", return_value={}):
            info = routes._server_tz_info()
    assert isinstance(info["server_tz_name"], str)
    assert info["server_tz_name"].strip() != ""
    assert isinstance(info["server_tz"], str) and info["server_tz"]


def test_server_tz_info_does_not_use_cached_hermes_time():
    """Structural guard: the helper must not import or call hermes_time's
    process-global cached resolver (review #7155: stale until reset_cache)."""
    ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    helper_start = ROUTES_PY.find("def _server_tz_info")
    helper_end = ROUTES_PY.find("\ndef ", helper_start + 1)
    helper = ROUTES_PY[helper_start:helper_end]
    # Exclude the docstring (which mentions hermes_time as a negative) — check
    # only the executable body.
    body_start = helper.find('"""', helper.find('"""') + 3) + 3
    body = helper[body_start:]
    assert "hermes_time" not in body, (
        "_server_tz_info must not use the cached hermes_time resolver"
    )
    assert "import hermes_time" not in ROUTES_PY[:helper_start]
    assert "_load_yaml_config_file" in helper, (
        "_server_tz_info must read config via the mtime-aware reader"
    )


if __name__ == "__main__":
    unittest.main()
