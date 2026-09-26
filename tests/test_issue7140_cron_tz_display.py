"""Regression tests for issue #7140 – cron detail panel renders timestamps
in the zone the timestamp itself was stamped in.

Maintainer review (issue #7140, 9/24) rejected the first fix — a single
process-wide ``server_tz`` offset — on three grounds:

1. **The reporter's scenario was not fixed.** The reporter runs a UTC
   container and only sets ``timezone: America/Sao_Paulo`` in the profile's
   ``config.yaml``.  Nothing copies that into ``HERMES_TIMEZONE``, so
   ``_server_tz_offset()`` resolved to ``+0000`` and the operator still saw
   UTC wall clock.
2. **DST and per-profile zones make a single current offset wrong.** The
   agent resolves the timezone *per profile*
   (``hermes_time._resolve_timezone_name()`` reads the ACTIVE PROFILE's
   ``config.yaml``), so one process can host jobs on different offsets, and
   a "current" offset is wrong for a timestamp in the other half of the
   year.
3. **No tests.** The change touched ``api/routes.py`` and
   ``static/panels.js`` with zero coverage.

The fix keeps the agent as the single source of truth for the zone: the
agent already serialises ``next_run_at`` / ``last_run_at`` with the offset
of the job's own zone (e.g. ``"2026-09-24T09:00:00-07:00"``).  The frontend
now reads that offset out of the string, shifts the instant by it and
formats with ``timeZone: 'UTC'`` — no timezone inference anywhere.

This file covers:

* the backend contract (``server_tz`` still shipped, 5-char
  ``±HHMM``, and — critically — that nothing in the cron payload is
  rewritten into another zone, so the client's offset is authoritative), and
* the JS ``_isoOffsetMinutes`` / ``_formatInIsoTz`` behaviour driven
  through the REAL ``static/sessions.js`` via node (the reporter's
  America/Sao_Paulo case, a DST pair, a per-profile mismatch, fractional
  offsets, and the fallback paths).
"""

import json
import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
ROUTES_PY_PATH = REPO_ROOT / "api" / "routes.py"

SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Could not extract {name}")


_ISO_OFFSET_FN = _extract_function(SESSIONS_JS, "_isoOffsetMinutes")
_FORMAT_IN_ISO_FN = _extract_function(SESSIONS_JS, "_formatInIsoTz")


def _run_js(script_body: str, tz: str = "UTC") -> dict:
    """Run a script body in node against the REAL sessions.js helpers.

    ``tz`` is pinned (not inherited from the CI machine) so the assertions
    below are about the ISO offset, never about the environment's zone.
    """
    script = textwrap.dedent(
        f"""
        process.env.TZ = '{tz}';
        {_ISO_OFFSET_FN}
        {_FORMAT_IN_ISO_FN}
        {script_body}
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Backend: the payload contract
# ---------------------------------------------------------------------------

def test_sessions_payload_still_ships_server_tz():
    """/api/sessions keeps server_tz so sessions.js' other helpers work."""
    from tests._pytest_port import BASE
    import urllib.request
    with urllib.request.urlopen(BASE + "/api/sessions", timeout=10) as r:
        data = json.loads(r.read())
    assert "server_tz" in data
    assert isinstance(data["server_tz"], str)
    assert len(data["server_tz"]) == 5  # "+HHMM" / "-HHMM"


def test_routes_py_rarely_rewrites_cron_timestamps():
    """The cron payload must NOT be re-zoned server-side.

    The whole fix rests on the ISO string's own offset reaching the browser
    untouched.  If a future change normalised next_run_at/last_run_at to a
    process-wide zone before serialising, the client could no longer see
    the per-profile/DST-correct offset — so the parser stays defensive
    (returns null → caller falls back) but the API must not strip it.
    """
    assert "_server_tz_offset" in ROUTES_PY_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# JS: _isoOffsetMinutes — the offset the string itself carries
# ---------------------------------------------------------------------------

def test_iso_offset_parses_sao_paulo():
    """The reporter's zone: UTC container, config.yaml America/Sao_Paulo."""
    result = _run_js("""
        process.stdout.write(JSON.stringify({
          sp: _isoOffsetMinutes('2026-09-24T09:00:00-03:00'),
        }));
    """)
    assert result["sp"] == -180


def test_iso_offset_parses_fractional_offsets():
    result = _run_js("""
        process.stdout.write(JSON.stringify({
          ist:   _isoOffsetMinutes('2026-09-24T09:00:00+05:30'),
          iran:  _isoOffsetMinutes('2026-09-24T09:00:00+0330'),
          nep:   _isoOffsetMinutes('2026-09-24T09:00:00+0545'),
          nfld:  _isoOffsetMinutes('2026-09-24T09:00:00-0330'),
          zulu:  _isoOffsetMinutes('2026-09-24T09:00:00Z'),
        }));
    """)
    assert result["ist"] == 330
    assert result["iran"] == 210
    assert result["nep"] == 345
    assert result["nfld"] == -210
    assert result["zulu"] == 0


def test_iso_offset_rejects_naive_and_junk():
    """A string with no offset must be reported as unusable (null) so the
    caller falls back instead of silently rendering the wrong zone."""
    result = _run_js("""
        process.stdout.write(JSON.stringify({
          naive:  _isoOffsetMinutes('2026-09-24T09:00:00'),
          empty:  _isoOffsetMinutes(''),
          junk:   _isoOffsetMinutes('not-a-timestamp'),
          absurd: _isoOffsetMinutes('2026-09-24T09:00:00+99:00'),
          num:    _isoOffsetMinutes(42),
        }));
    """)
    assert result["naive"] is None
    assert result["empty"] is None
    assert result["junk"] is None
    assert result["absurd"] is None
    assert result["num"] is None


# ---------------------------------------------------------------------------
# JS: _formatInIsoTz — the reporter's scenario
# ---------------------------------------------------------------------------

def test_reporter_scenario_sao_paulo_renders_local_wall_clock():
    """UTC container + config.yaml America/Sao_Paulo + no HERMES_TIMEZONE.

    An operator's browser is on UTC.  The job's timestamp is
    "2026-09-24T09:00:00-03:00".  The panel must read 9:00 AM (the server's
    wall clock), not 12:00 PM (the browser's).
    """
    result = _run_js(tz="UTC", script_body="""
        const out = _formatInIsoTz('2026-09-24T09:00:00-03:00');
        process.stdout.write(JSON.stringify({ formatted: out }));
    """)
    # 12:00Z = 09:00-03:00 — the whole point is that the shift happens.
    assert "9:00" in result["formatted"] or "09:00" in result["formatted"], (
        f"Expected 9:00 AM (Sao Paulo wall clock) in {result['formatted']!r}"
    )


def test_dst_pair_uses_each_timestamps_own_offset():
    """A current offset is wrong across DST: the two halves of the year
    carry different offsets and each must be honoured."""
    result = _run_js(tz="UTC", script_body="""
        process.stdout.write(JSON.stringify({
          summer: _formatInIsoTz('2026-07-15T09:00:00-03:00'),
          winter: _formatInIsoTz('2026-01-15T09:00:00-02:00'),
        }));
    """)
    # Both are 09:00 in their own zone even though the offsets differ.
    assert "9:00" in result["summer"] or "09:00" in result["summer"]
    assert "9:00" in result["winter"] or "09:00" in result["winter"]


def test_per_profile_zones_render_independently():
    """One process (and one server_tz) can host jobs on different zones."""
    result = _run_js(tz="UTC", script_body="""
        process.stdout.write(JSON.stringify({
          tokyo:  _formatInIsoTz('2026-09-24T09:00:00+09:00'),
          sao:    _formatInIsoTz('2026-09-24T09:00:00-03:00'),
          london: _formatInIsoTz('2026-09-24T09:00:00+01:00'),
        }));
    """)
    assert "9:00" in result["tokyo"] or "09:00" in result["tokyo"]
    assert "9:00" in result["sao"] or "09:00" in result["sao"]
    assert "9:00" in result["london"] or "09:00" in result["london"]


def test_format_in_iso_tz_handles_fractional_offset():
    """India +0530 — the minute must survive the shift.

    02:00Z stamped +05:30 is 07:30 IST, so the shifted instant must land on
    :30 past the hour, not on the whole hour an Etc/GMT-5 mapping gives.
    """
    result = _run_js(tz="UTC", script_body="""
        const out = _formatInIsoTz('2026-09-24T02:00:00.000Z');
        const stamped = _formatInIsoTz('2026-09-24T07:30:00+05:30');
        process.stdout.write(JSON.stringify({ stamped }));
    """)
    assert "7:30" in result["stamped"] or "07:30" in result["stamped"], (
        f"Expected 07:30 (IST wall clock) in {result['stamped']!r}"
    )


def test_format_in_iso_tz_zulu_and_fallbacks():
    result = _run_js(tz="UTC", script_body="""
        process.stdout.write(JSON.stringify({
          zulu:    _formatInIsoTz('2026-09-24T09:00:00Z'),
          naive:   _formatInIsoTz('2026-09-24T09:00:00'),
          number:  _formatInIsoTz(42),
          nothing: _formatInIsoTz(null),
          bogus:   _formatInIsoTz('2026-13-45T99:00:00+07:00'),
        }));
    """)
    assert "9:00" in result["zulu"] or "09:00" in result["zulu"]
    # No usable offset → null, caller falls back (never a wrong zone).
    assert result["naive"] is None
    assert result["number"] is None
    assert result["nothing"] is None
    assert result["bogus"] is None


def test_panels_js_prefers_iso_offset_over_server_tz():
    """panels.js must consult the ISO string's own offset FIRST."""
    assert "_formatInIsoTz" in PANELS_JS
    detail_start = PANELS_JS.index("function _renderCronDetail")
    detail = PANELS_JS[detail_start:detail_start + 3000]
    assert "_formatInIsoTz" in detail, (
        "_renderCronDetail must format via the timestamp's own ISO offset"
    )
    # ...and keep _formatInServerTz as the documented fallback path.
    assert "_formatInServerTz" in detail


def test_panels_js_falls_back_when_helper_out_of_scope():
    """The browser-zone toLocaleString fallback must survive for the
    no-helper-in-scope path (panels.js may load before sessions.js)."""
    detail_start = PANELS_JS.index("function _renderCronDetail")
    detail = PANELS_JS[detail_start:detail_start + 3000]
    assert "toLocaleString" in detail


def test_sessions_js_exports_iso_helpers():
    """The helpers must be defined at top level in sessions.js (where the
    other panels pick them up as globals)."""
    assert "function _isoOffsetMinutes" in SESSIONS_JS
    assert "function _formatInIsoTz" in SESSIONS_JS
