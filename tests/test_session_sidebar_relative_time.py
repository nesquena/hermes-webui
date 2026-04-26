import json
import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


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


def _run_session_time_case(script_body: str) -> dict:
    functions = "\n\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionTimestampMs",
            "_localDayOrdinal",
            "_sessionCalendarBoundaries",
            "_formatSessionDate",
            "_formatRelativeSessionTime",
            "_sessionTimeBucketLabel",
        )
    )
    script = textwrap.dedent(
        f"""
        process.env.TZ = 'UTC';
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


def test_session_sidebar_js_has_dynamic_relative_time_helpers():
    assert "function _sessionTimestampMs" in SESSIONS_JS
    assert "function _sessionCalendarBoundaries" in SESSIONS_JS
    assert "function _formatRelativeSessionTime" in SESSIONS_JS
    assert "function _sessionTimeBucketLabel" in SESSIONS_JS
    assert "session_time_bucket_last_week" in SESSIONS_JS
    assert "session_time_bucket_this_week" in SESSIONS_JS
    assert "session_time_bucket_older" in SESSIONS_JS


def test_session_sidebar_renders_relative_time_and_meta_rows():
    # session-time element was removed from sessions.js in v0.50.40 to
    # give session titles full width — the CSS class is kept but set to display:none.
    # session-meta / metaBits were removed when we dropped message-count, model, and
    # source-tag badges from the sidebar (design round 2).
    assert "orderedSessions" in SESSIONS_JS
    assert ".session-time" in STYLE_CSS
    assert ".session-title-row" in STYLE_CSS
    assert ".session-item.active .session-title" in STYLE_CSS
    assert "|| _sessionTimeBucketLabel" not in SESSIONS_JS
    assert "const ONE_DAY=86400000;" not in SESSIONS_JS


def test_session_timestamp_prefers_last_message_at_over_metadata_updated_at():
    result = _run_session_time_case(
        """
        const session = {
          created_at: 1776441348,
          updated_at: 1777086443,
          last_message_at: 1776441972,
        };
        process.stdout.write(JSON.stringify({
          timestampMs: _sessionTimestampMs(session),
        }));
        """
    )
    assert result["timestampMs"] == 1776441972 * 1000


def test_relative_time_uses_calendar_boundaries_and_year_for_old_sessions():
    result = _run_session_time_case(
        """
        const now = Date.UTC(2026, 3, 15, 1, 0, 0);
        const mondayLate = Date.UTC(2026, 3, 13, 23, 0, 0);
        const oldSession = Date.UTC(2024, 2, 5, 12, 0, 0);
        process.stdout.write(JSON.stringify({
          relative: _formatRelativeSessionTime(mondayLate, now),
          bucket: _sessionTimeBucketLabel(mondayLate, now),
          oldDate: _formatRelativeSessionTime(oldSession, now),
        }));
        """
    )
    assert result["relative"] == "2d"
    assert result["bucket"] == "This week"
    assert "2024" in result["oldDate"]


def test_relative_time_today_bucket():
    """Session from 2 hours ago should bucket as 'Today'."""
    result = _run_session_time_case(
        """
        const now = Date.UTC(2026, 3, 15, 14, 0, 0);
        const twoHoursAgo = now - 2 * 60 * 60 * 1000;
        process.stdout.write(JSON.stringify({
          relative: _formatRelativeSessionTime(twoHoursAgo, now),
          bucket: _sessionTimeBucketLabel(twoHoursAgo, now),
        }));
        """
    )
    assert result["relative"] == "2h"
    assert result["bucket"] == "Today"


def test_relative_time_handles_just_now_and_dst_safe_yesterday_boundary():
    result = _run_session_time_case(
        """
        const now = Date.UTC(2026, 2, 9, 12, 0, 0);
        const justNow = now - 30 * 1000;
        const yesterday = Date.UTC(2026, 2, 8, 23, 30, 0);
        process.stdout.write(JSON.stringify({
          justNow: _formatRelativeSessionTime(justNow, now),
          yesterday: _formatRelativeSessionTime(yesterday, now),
          yesterdayBucket: _sessionTimeBucketLabel(yesterday, now),
        }));
        """
    )
    assert result["justNow"] == "1m"
    assert result["yesterday"] == "1d"
    assert result["yesterdayBucket"] == "Yesterday"


def test_relative_time_strings_are_localized_in_english_and_spanish_bundles():
    for key in (
        "session_time_unknown",
        "session_time_minutes_ago",
        "session_time_hours_ago",
        "session_time_days_ago",
        "session_time_last_week",
        "session_time_bucket_today",
        "session_time_bucket_yesterday",
        "session_time_bucket_this_week",
        "session_time_bucket_last_week",
        "session_time_bucket_older",
    ):
        assert key in I18N_JS
