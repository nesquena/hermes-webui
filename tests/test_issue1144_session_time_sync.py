"""Regression tests for issue #1144 – session time sync with system time.

Root cause: The WebUI used Date.now() (client-side clock) as the reference
for all relative-time calculations ("2 hours ago", "Today", "Yesterday", etc.).
If the server clock and client clock are out of sync (e.g. WSL clock drift,
Docker container TZ mismatch), timestamps appear wrong.

Fix: The /api/sessions response now includes ``server_time`` (epoch seconds)
and ``server_tz`` (offset string like "+0800").  The JS computes
``_serverTimeDelta = Date.now() - server_time * 1000`` once per session-list
fetch, then every time helper uses ``_serverNowMs()`` (which returns
``Date.now() - _serverTimeDelta``) instead of bare ``Date.now()``.
"""

import json
import pathlib
import subprocess
import textwrap
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend: /api/sessions includes server_time and server_tz
# ---------------------------------------------------------------------------

def test_sessions_endpoint_includes_server_time_and_tz():
    """GET /api/sessions must return server_time (float) and server_tz (str)."""
    from tests._pytest_port import BASE
    import urllib.request
    with urllib.request.urlopen(BASE + "/api/sessions", timeout=10) as r:
        data = json.loads(r.read())
    assert "server_time" in data
    assert "server_tz" in data
    # server_time should be a recent epoch seconds value
    assert isinstance(data["server_time"], float)
    assert data["server_time"] > 1_700_000_000  # after 2023
    # Should be close to time.time()
    assert abs(data["server_time"] - time.time()) < 5
    # server_tz should be an offset string
    assert isinstance(data["server_tz"], str)
    assert len(data["server_tz"]) == 5  # "+HHMM" or "-HHMM"


def test_server_time_allows_clock_skew_compensation():
    """server_time lets the client detect clock skew relative to the server."""
    from tests._pytest_port import BASE
    import urllib.request
    before = time.time()
    with urllib.request.urlopen(BASE + "/api/sessions", timeout=10) as r:
        data = json.loads(r.read())
    after = time.time()
    server_time = data["server_time"]
    # The server_time should be between our before and after timestamps
    assert before <= server_time <= after


# ---------------------------------------------------------------------------
# JS: _serverNowMs compensates for clock skew
# ---------------------------------------------------------------------------

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


def _run_time_case(script_body: str, tz: str = "UTC") -> dict:
    """Extract time-related functions and run a script body via Node.js."""
    functions = "\n\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionTimestampMs",
            "_localDayOrdinal",
            "_serverNowMs",
            "_serverTzOptions",
            "_sessionCalendarBoundaries",
            "_formatSessionDate",
            "_formatRelativeSessionTime",
            "_sessionTimeBucketLabel",
        )
    )
    script = textwrap.dedent(
        f"""
        process.env.TZ = '{tz}';
        let _serverTimeDelta = 0;
        let _serverTz = '';
        const translations = {{
          session_time_unknown: 'Unknown',
          session_time_minutes_ago: (n) => `${{n}}m`,
          session_time_hours_ago: (n) => `${{n}}h`,
          session_time_days_ago: (n) => `${{n}}d`,
          session_time_last_week: '1w',
          session_time_bucket_today: 'Today',
          session_time_bucket_yesterday: 'Yesterday',
          session_time_bucket_this_week: 'This week',
          session_time_bucket_last_week: 'Last week',
          session_time_bucket_older: 'Older',
        }};
        function t(key, ...args) {{
          const val = translations[key];
          return typeof val === 'function' ? val(...args) : val;
        }}
        {functions}
        {script_body}
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_server_now_ms_defaults_to_date_now_when_no_skew():
    """Without skew, _serverNowMs() should equal Date.now()."""
    result = _run_time_case(
        """
        const before = Date.now();
        const serverNow = _serverNowMs();
        const after = Date.now();
        process.stdout.write(JSON.stringify({
          serverNow,
          closeToNow: serverNow >= before && serverNow <= after,
        }));
        """
    )
    assert result["closeToNow"] is True


def test_server_now_ms_compensates_positive_skew():
    """If server is behind client (skew > 0), _serverNowMs() subtracts the delta.

    Uses a small tolerance window (±5 ms) because two consecutive Date.now() calls
    inside Node.js can differ by 1-2 ms on a loaded system, causing `diff === 3600000`
    to fail intermittently even though the compensation logic is correct.
    """
    result = _run_time_case(
        """
        // Simulate: client clock is 3600s (1 hour) ahead of server
        _serverTimeDelta = 3600 * 1000;
        const t0 = Date.now();
        const serverNow = _serverNowMs();  // internally calls Date.now() again
        const t1 = Date.now();
        // Use the midpoint of t0..t1 to absorb the tiny time-of-call delta
        const diffMs = ((t0 + t1) / 2) - serverNow;
        process.stdout.write(JSON.stringify({
          diffMs: Math.round(diffMs),
          isOneHour: Math.abs(diffMs - 3600000) < 5,
        }));
        """
    )
    assert result["isOneHour"] is True, (
        f"Expected diff ≈ 3600000 ms, got {result['diffMs']} ms. "
        "The skew compensation is broken."
    )
    assert abs(result["diffMs"] - 3_600_000) < 5


def test_server_now_ms_compensates_negative_skew():
    """If server is ahead of client (skew < 0), _serverNowMs() adds the delta.

    Uses midpoint averaging with ±5 ms tolerance to avoid intermittent failures
    caused by consecutive Date.now() calls returning different values under CPU load.
    (Same fix as the positive-skew test above.)
    """
    result = _run_time_case(
        """
        // Simulate: client clock is 7200s (2 hours) behind server
        _serverTimeDelta = -7200 * 1000;
        const t0 = Date.now();
        const serverNow = _serverNowMs();  // internally calls Date.now() at T1 >= T0
        const t1 = Date.now();
        // serverNow = T1 + 7200000; clientNow ≈ midpoint(T0,T1)
        const diffMs = serverNow - ((t0 + t1) / 2);
        process.stdout.write(JSON.stringify({
          diffMs: Math.round(diffMs),
          isTwoHours: Math.abs(diffMs - 7200000) < 5,
        }));
        """
    )
    assert result["isTwoHours"] is True, (
        f"Expected diff ≈ 7200000 ms, got {result['diffMs']} ms. "
        "The negative-skew compensation is broken."
    )
    assert abs(result["diffMs"] - 7_200_000) < 5


def test_relative_time_uses_server_clock():
    """_formatRelativeSessionTime uses _serverNowMs() when nowMs is not passed."""
    result = _run_time_case(
        """
        // Simulate server 8 hours behind client (common WSL scenario).
        // Pin Date.now() to a clock-stable instant well away from any UTC
        // calendar boundary so the test does not depend on what time CI
        // happens to run. With _serverTimeDelta = +8h, _serverNowMs() returns
        // (Date.now() - 8h). If Date.now() were unpinned and CI ran near
        // 08:00 UTC, the projected server time would be ~midnight and the
        // "5 minutes ago" subtraction would silently cross into yesterday.
        const _origNow = Date.now;
        Date.now = () => new Date('2026-05-06T20:00:00Z').getTime();
        _serverTimeDelta = 8 * 3600 * 1000;
        // Session created 5 minutes ago in server time
        const serverNow = _serverNowMs();
        const fiveMinAgo = serverNow - 5 * 60 * 1000;
        process.stdout.write(JSON.stringify({
          relative: _formatRelativeSessionTime(fiveMinAgo),
          bucket: _sessionTimeBucketLabel(fiveMinAgo),
        }));
        Date.now = _origNow;
        """
    )
    # Without compensation, client thinks this session is 8h5m ago.
    # With compensation, it correctly shows "5m".
    assert result["relative"] == "5m"
    assert result["bucket"] == "Today"


def test_session_bucket_uses_server_clock():
    """_sessionTimeBucketLabel uses _serverNowMs() for Today/Yesterday boundaries."""
    result = _run_time_case(
        """
        // Pin the client clock away from midnight so this regression test does
        // not depend on when CI happens to run. With an 8-hour positive server
        // skew, an unpinned Date.now() near 16:00 UTC makes serverNow cross
        // midnight and turns "2 hours ago" into the prior calendar day.
        const fixedClientNow = Date.UTC(2026, 3, 15, 12, 0, 0);
        Date.now = () => fixedClientNow;
        // Simulate server 8 hours ahead of client
        _serverTimeDelta = -8 * 3600 * 1000;
        const serverNow = _serverNowMs();
        // Session created 2 hours ago in server time → should be Today
        const twoHoursAgo = serverNow - 2 * 3600 * 1000;
        // Session created 26 hours ago → should be Yesterday
        const yesterday = serverNow - 26 * 3600 * 1000;
        process.stdout.write(JSON.stringify({
          todayBucket: _sessionTimeBucketLabel(twoHoursAgo),
          yesterdayBucket: _sessionTimeBucketLabel(yesterday),
          todayRelative: _formatRelativeSessionTime(twoHoursAgo),
        }));
        """
    )
    assert result["todayBucket"] == "Today"
    assert result["yesterdayBucket"] == "Yesterday"
    assert result["todayRelative"] == "2h"


def test_explicit_now_param_overrides_server_clock():
    """Passing nowMs explicitly should still work (backward compat)."""
    result = _run_time_case(
        """
        _serverTimeDelta = 8 * 3600 * 1000;  // large skew
        const explicitNow = Date.UTC(2026, 3, 15, 14, 0, 0);
        const twoHoursAgo = explicitNow - 2 * 3600 * 1000;
        process.stdout.write(JSON.stringify({
          relative: _formatRelativeSessionTime(twoHoursAgo, explicitNow),
          bucket: _sessionTimeBucketLabel(twoHoursAgo, explicitNow),
        }));
        """
    )
    # Explicit now should be used, not server clock
    assert result["relative"] == "2h"
    assert result["bucket"] == "Today"


# ---------------------------------------------------------------------------
# JS: _serverTzOptions builds correct timeZone option
# ---------------------------------------------------------------------------

def test_server_tz_options_positive_offset():
    result = _run_time_case(
        """
        _serverTz = '+0800';
        const opts = _serverTzOptions();
        process.stdout.write(JSON.stringify({
          tz: opts ? opts.timeZone : null,
        }));
        """
    )
    assert result["tz"] == "Etc/GMT-8"


def test_server_tz_options_negative_offset():
    result = _run_time_case(
        """
        _serverTz = '-0500';
        const opts = _serverTzOptions();
        process.stdout.write(JSON.stringify({
          tz: opts ? opts.timeZone : null,
        }));
        """
    )
    assert result["tz"] == "Etc/GMT+5"


def test_server_tz_options_utc_returns_undefined():
    result = _run_time_case(
        """
        _serverTz = '+0000';
        const opts = _serverTzOptions();
        process.stdout.write(JSON.stringify({
          isUndefined: opts === undefined,
          isNull: opts === null,
          type: typeof opts,
        }));
        """
    )
    assert result["isUndefined"] is True
    assert result["isNull"] is False
    assert result["type"] == "undefined"


def test_server_tz_options_empty_returns_undefined():
    result = _run_time_case(
        """
        _serverTz = '';
        const opts = _serverTzOptions();
        process.stdout.write(JSON.stringify({
          isUndefined: opts === undefined,
        }));
        """
    )
    assert result["isUndefined"] is True


# ---------------------------------------------------------------------------
# JS: _formatMessageFooterTimestamp uses server timezone
# ---------------------------------------------------------------------------

def _extract_ui_function(name: str) -> str:
    return _extract_function(UI_JS, name)


def _extract_is_same_local_day() -> str:
    """Extract _isSameLocalDay from ui.js (helper used by _formatMessageFooterTimestamp)."""
    marker = "function _isSameLocalDay("
    start = UI_JS.index(marker)
    brace_start = UI_JS.index("{", start)
    depth = 0
    for idx in range(brace_start, len(UI_JS)):
        ch = UI_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : idx + 1]
    raise AssertionError("Could not extract _isSameLocalDay")


def test_message_footer_timestamp_uses_server_tz():
    """_formatMessageFooterTimestamp should use _formatInServerTz for display."""
    is_same_day_fn = _extract_is_same_local_day()
    fmt_fn = _extract_ui_function("_formatMessageFooterTimestamp")
    script = textwrap.dedent(
        f"""
        process.env.TZ = 'America/New_York';
        let _serverTimeDelta = 0;
        let _serverTz = '+0800';
        // Stub _formatInServerTz with the same offset-arithmetic semantics
        // as the real implementation in sessions.js.
        function _formatInServerTz(date, options) {{
          if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') {{
            return date.toLocaleString(undefined, options);
          }}
          const m = _serverTz.match(/^([+-])(\\d{{2}})(\\d{{2}})$/);
          if (!m) return date.toLocaleString(undefined, options);
          const sign = m[1] === '+' ? 1 : -1;
          const offsetMin = sign * (parseInt(m[2]) * 60 + parseInt(m[3]));
          const adjusted = new Date(date.getTime() + offsetMin * 60 * 1000);
          return adjusted.toLocaleString(undefined, {{ ...options, timeZone: 'UTC' }});
        }}
        {is_same_day_fn}
        {fmt_fn}
        // Timestamp for 2026-03-29 02:00:00 UTC = 10:00 in UTC+8
        const tsVal = 1774749600;
        const result = _formatMessageFooterTimestamp(tsVal);
        process.stdout.write(JSON.stringify({{ formatted: result }}));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    # Should display in UTC+8, not America/New_York.  Keep this assertion
    # locale-independent: Node may render the same wall-clock as "10:00 AM"
    # in en-US or "3月29日 10:00" in Japanese locales.
    # 2026-03-29 02:00 UTC = 10:00 in UTC+8; America/New_York would be 22:00.
    assert "10:00" in data["formatted"], (
        f"Expected a UTC+8 10:00 wall-clock in {data['formatted']!r}"
    )
    assert "22:00" not in data["formatted"] and "10:00 PM" not in data["formatted"], (
        f"Formatted timestamp appears to use local/New_York time: {data['formatted']!r}"
    )


def test_message_footer_timestamp_handles_fractional_offset():
    """_formatMessageFooterTimestamp must correctly format in IST (+0530) and
    other half-hour offsets — Etc/GMT can't express these but offset
    arithmetic in _formatInServerTz handles them correctly. Affects ~1.5B
    users in India, Iran, Newfoundland, Nepal, Sri Lanka, etc."""
    is_same_day_fn = _extract_is_same_local_day()
    fmt_fn = _extract_ui_function("_formatMessageFooterTimestamp")
    script = textwrap.dedent(
        f"""
        process.env.TZ = 'UTC';
        let _serverTimeDelta = 0;
        let _serverTz = '+0530';  // India IST
        function _formatInServerTz(date, options) {{
          if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') {{
            return date.toLocaleString(undefined, options);
          }}
          const m = _serverTz.match(/^([+-])(\\d{{2}})(\\d{{2}})$/);
          if (!m) return date.toLocaleString(undefined, options);
          const sign = m[1] === '+' ? 1 : -1;
          const offsetMin = sign * (parseInt(m[2]) * 60 + parseInt(m[3]));
          const adjusted = new Date(date.getTime() + offsetMin * 60 * 1000);
          return adjusted.toLocaleString(undefined, {{ ...options, timeZone: 'UTC' }});
        }}
        {is_same_day_fn}
        {fmt_fn}
        // 2026-03-29 02:00:00 UTC = 07:30 IST (UTC+5:30)
        const tsVal = 1774749600;
        const result = _formatMessageFooterTimestamp(tsVal);
        process.stdout.write(JSON.stringify({{ formatted: result }}));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    # 2026-03-29 02:00 UTC = 07:30 IST. Old Etc/GMT-5 mapping would have shown 07:00.
    # Accept either "07:30" or "7:30" (en-US uses hour:'numeric' for non-same-day).
    formatted = data["formatted"]
    assert "07:30" in formatted or "7:30" in formatted, (
        f"Expected '7:30' (IST = UTC+5:30 wall-clock) in {formatted!r}; "
        "Etc/GMT-5 path would show 7:00 — off by 30 min."
    )
    # And explicitly NOT the broken Etc/GMT-5 output (07:00 / 7:00 with 0 minutes).
    assert ":00" not in formatted.split("M")[0], (
        f"Output contains ':00' which would be the off-by-30-min Etc/GMT-5 result; "
        f"got {formatted!r}"
    )


def test_message_footer_timestamp_falls_back_without_server_tz():
    """Without _serverTzOptions, should use browser timezone (no crash)."""
    is_same_day_fn = _extract_is_same_local_day()
    fmt_fn = _extract_ui_function("_formatMessageFooterTimestamp")
    script = textwrap.dedent(
        f"""
        process.env.TZ = 'UTC';
        // _serverTzOptions is not defined — simulates sessions.js not loaded
        let _serverTimeDelta = 0;
        {is_same_day_fn}
        {fmt_fn}
        const tsVal = 1774749600;  // 2026-04-28 10:00 UTC
        const result = _formatMessageFooterTimestamp(tsVal);
        process.stdout.write(JSON.stringify({{ formatted: result, hasValue: result.length > 0 }}));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    assert data["hasValue"] is True


# ---------------------------------------------------------------------------
# JS: sessions.js contains the compensation variables and helpers
# ---------------------------------------------------------------------------

def test_sessions_js_has_server_time_compensation_vars():
    assert "_serverTimeDelta" in SESSIONS_JS
    assert "_serverTz" in SESSIONS_JS
    assert "function _serverNowMs()" in SESSIONS_JS
    assert "function _serverTzOptions()" in SESSIONS_JS


def test_sessions_js_captures_server_time_on_fetch():
    assert "sessData.server_time" in SESSIONS_JS
    assert "sessData.server_tz" in SESSIONS_JS
    assert "_serverTimeDelta = Date.now()" in SESSIONS_JS


def test_sessions_js_uses_server_now_in_time_functions():
    """All time formatting functions should use _serverNowMs() as default."""
    assert "_serverNowMs()" in SESSIONS_JS
    # Ensure the old pattern `Date.now()` is NOT the default in these functions
    assert "nowMs = Date.now()" not in SESSIONS_JS
    # _serverNowMs() should be used as fallback in time formatting functions
    assert "nowMs || _serverNowMs()" in SESSIONS_JS


def test_ui_js_message_timestamp_uses_server_tz():
    """ui.js timestamp formatters should reference the server-tz helpers
    so they pick up the server's wall-clock time (with correct fractional
    offset handling) rather than always rendering in browser TZ."""
    # _formatInServerTz is the canonical helper that handles both whole-hour
    # and fractional offsets (e.g. India +0530). _serverTzOptions is the
    # whole-hour fast path; either reference indicates server-tz awareness.
    assert "_formatInServerTz" in UI_JS or "_serverTzOptions" in UI_JS, (
        "ui.js must reference one of the server-tz helpers so message "
        "timestamps render in the server's wall-clock time"
    )


def test_sessions_js_has_format_in_server_tz_helper():
    """_formatInServerTz must exist and use offset arithmetic so fractional
    offsets (India +0530, Iran +0330, etc.) format correctly."""
    assert "function _formatInServerTz" in SESSIONS_JS, (
        "_formatInServerTz must be defined to handle fractional-hour "
        "offsets that Etc/GMT cannot express"
    )
    # Find the function body
    start = SESSIONS_JS.find("function _formatInServerTz")
    end = SESSIONS_JS.find("\n}", start) + 2
    body = SESSIONS_JS[start:end]
    # Offset arithmetic + timeZone:'UTC' is the correct strategy
    assert "timeZone: 'UTC'" in body or 'timeZone: "UTC"' in body, (
        "_formatInServerTz must format in UTC after applying the offset "
        "via arithmetic — that's how fractional offsets work correctly"
    )
    assert "60 * 1000" in body or "* 60_000" in body, (
        "_formatInServerTz must convert the offset minutes to milliseconds"
    )
