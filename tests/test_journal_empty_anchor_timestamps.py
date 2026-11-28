"""Corrupt timestamps and empty journal anchors must not look like fresh turns.

Two independent bugs in the vanilla client and session summary:

* A unix value at or above 4e9 (milliseconds stored as seconds, or other
  garbage) is in the future. The sidebar clamps a negative age to one minute,
  so the session reads as "1m". The same value opens a nonsense date separator.
* ``ensure_assistant_anchor`` persists an assistant row with no text so
  recovered tool cards have a host. That placeholder is not a turn. If it is
  the tail, ``last_message_at`` becomes "now" and the transcript prints
  Today/Yesterday under the last real answer.

``dedupe_existing`` stays false. Call sites that want dedup pass true;
changing the default is a different repair.
"""
import inspect
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

import api.models as models
from api.models import Session, _append_journaled_partial_output, _last_message_timestamp, _message_timestamp

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


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


def _run_node(script_body: str) -> dict:
    script = textwrap.dedent(
        f"""
        process.env.TZ = 'UTC';
        {script_body}
        """
    )
    env = os.environ.copy()
    env["TZ"] = "UTC"
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, env=env)
    return json.loads(proc.stdout)


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def test_journal_recovery_keeps_dedupe_existing_default_false():
    default = inspect.signature(_append_journaled_partial_output).parameters["dedupe_existing"].default
    assert default is False


def test_message_timestamp_rejects_non_finite_and_millisecond_scale():
    assert _message_timestamp({"timestamp": 200.0}) == 200.0
    assert _message_timestamp({"_ts": 1_776_441_972}) == 1_776_441_972.0
    assert _message_timestamp({"timestamp": 5e12}) is None
    assert _message_timestamp({"timestamp": 4_000_000_000}) is None
    assert _message_timestamp({"timestamp": float("nan")}) is None
    assert _message_timestamp({"timestamp": float("inf")}) is None
    assert _message_timestamp({"timestamp": -20}) is None
    assert _message_timestamp({"timestamp": 0}) is None
    assert _message_timestamp("nope") is None


def test_last_message_at_skips_empty_recovered_anchor_and_uses_real_turn():
    messages = [
        {"role": "user", "content": "question", "timestamp": 200.0},
        {"role": "assistant", "content": "the answer", "timestamp": 201.0},
        {
            "role": "assistant",
            "content": "",
            "timestamp": 1_776_441_972.0,
            "_recovered_from_run_journal": True,
            "_recovered_stream_id": "stream-1",
        },
    ]
    assert _last_message_timestamp(messages) == 201.0


def test_last_message_at_skips_blank_and_whitespace_anchors_past_the_tail_window():
    messages = [
        {"role": "assistant", "content": "still the answer", "timestamp": 150.0},
    ]
    for i in range(9):
        messages.append(
            {
                "role": "assistant",
                "content": "   \n" if i % 2 else "",
                "timestamp": 1_000_000 + i,
                "_recovered_from_run_journal": True,
            }
        )
    assert _last_message_timestamp(messages) == 150.0


def test_last_message_at_keeps_recovered_text_reasoning_and_structured_text():
    assert _last_message_timestamp(
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {
                "role": "assistant",
                "content": "recovered sentence",
                "timestamp": 20.0,
                "_recovered_from_run_journal": True,
            },
        ]
    ) == 20.0
    assert _last_message_timestamp(
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "from parts"}],
                "timestamp": 30.0,
                "_recovered_from_run_journal": True,
            },
        ]
    ) == 30.0
    assert _last_message_timestamp(
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "content": "from output_text"}],
                "timestamp": 35.0,
                "_recovered_from_run_journal": True,
            },
        ]
    ) == 35.0
    assert _last_message_timestamp(
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "still a visible thought",
                "timestamp": 40.0,
                "_recovered_from_run_journal": True,
            },
        ]
    ) == 40.0


def test_last_message_at_ignores_millisecond_garbage_on_a_real_message():
    assert _last_message_timestamp(
        [
            {"role": "assistant", "content": "kept", "timestamp": 200.0},
            {"role": "user", "content": "corrupt clock", "timestamp": 5e12},
        ]
    ) == 200.0


def test_compact_last_message_at_uses_the_answer_not_the_empty_anchor():
    session = Session(
        session_id="sess_anchor_tail",
        title="Anchor tail",
        updated_at=300.0,
        messages=[
            {"role": "user", "content": "question", "timestamp": 200.0},
            {"role": "assistant", "content": "the answer", "timestamp": 201.0},
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1_776_441_972.0,
                "_recovered_from_run_journal": True,
            },
        ],
    )
    compact = session.compact()
    assert compact["updated_at"] == 300.0
    assert compact["last_message_at"] == 201.0


def test_sidebar_timestamp_falls_through_insane_candidates_and_keeps_fixtures():
    sessions = "\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in ("_serverNowMs", "_sessionTimestampMs")
    )
    result = _run_node(
        f"""
        let _serverTimeDelta = 0;
        {sessions}
        const nowSec = Date.now() / 1000;
        const sane = 1776441972;
        const skewSec = nowSec + 30;
        process.stdout.write(JSON.stringify({{
          garbage: _sessionTimestampMs({{
            last_message_at: 5e12,
            updated_at: sane,
          }}),
          future: _sessionTimestampMs({{
            last_message_at: nowSec + 10 * 24 * 3600,
            updated_at: sane,
          }}),
          skew: _sessionTimestampMs({{
            last_message_at: skewSec,
            updated_at: sane,
          }}),
          skewExpected: skewSec * 1000,
          fixture: _sessionTimestampMs({{
            last_message_at: 10,
            updated_at: 20,
          }}),
          onlyGarbage: _sessionTimestampMs({{
            last_message_at: 5e12,
            updated_at: -5,
          }}),
          preference: _sessionTimestampMs({{
            updated_at: 1777086443,
            last_message_at: sane,
          }}),
        }}));
        """
    )
    assert result["garbage"] == 1776441972 * 1000
    assert result["future"] == 1776441972 * 1000
    assert result["skew"] == result["skewExpected"]
    assert result["fixture"] == 10_000
    assert result["onlyGarbage"] == 0
    assert result["preference"] == 1776441972 * 1000


def test_sidebar_does_not_label_a_far_future_timestamp_as_one_minute():
    functions = "\n\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_localDayOrdinal",
            "_sessionCalendarBoundaries",
            "_formatSessionDate",
            "_formatRelativeSessionTime",
        )
    )
    result = _run_node(
        f"""
        const translations = {{
          session_time_unknown: 'Unknown',
          session_time_minutes_ago: (n) => `${{n}}m`,
          session_time_hours_ago: (n) => `${{n}}h`,
          session_time_days_ago: (n) => `${{n}}d`,
          session_time_last_week: '1w',
        }};
        function t(key, ...args) {{
          const val = translations[key];
          return typeof val === 'function' ? val(...args) : val;
        }}
        function _serverNowMs() {{ return Date.now(); }}
        {functions}
        const now = Date.UTC(2026, 9, 30, 12, 0, 0);
        const year2065 = 3e9 * 1000;
        const skew = now + 30 * 1000;
        const oneMinute = now - 20 * 1000;
        process.stdout.write(JSON.stringify({{
          future: _formatRelativeSessionTime(year2065, now),
          skew: _formatRelativeSessionTime(skew, now),
          recent: _formatRelativeSessionTime(oneMinute, now),
        }}));
        """
    )
    assert result["future"] == "Unknown"
    assert result["skew"] == "1m"
    assert result["recent"] == "1m"


def test_date_separators_skip_empty_recovered_anchors_and_garbage_times():
    names = (
        "_fmtDateSep",
        "_recoveredAnchorText",
        "_isEmptyRecoveredJournalAnchor",
        "_messageDateSepDayKey",
        "_nextDateSepText",
    )
    functions = "\n\n".join(
        [_extract_function(SESSIONS_JS, "_serverNowMs")]
        + [_extract_function(UI_JS, name) for name in names]
    )
    result = _run_node(
        f"""
        let _serverTimeDelta = 0;
        {functions}
        function labels(messages) {{
          const out = [];
          let prev = null;
          for (const message of messages) {{
            const step = _nextDateSepText(message, prev);
            if (step.text) out.push(step.text);
            prev = step.key;
          }}
          return out;
        }}
        const noon = new Date();
        noon.setUTCHours(12, 0, 0, 0);
        const day = 86400;
        const today = Math.floor(noon.getTime() / 1000);
        const yesterday = today - day;
        const twoDays = today - 2 * day;
        const messages = [
          {{role: 'user', content: 'older question', timestamp: twoDays}},
          {{role: 'assistant', content: 'the answer', timestamp: yesterday}},
          {{
            role: 'assistant',
            content: '',
            timestamp: today,
            _recovered_from_run_journal: true,
          }},
          {{
            role: 'assistant',
            content: '   ',
            timestamp: twoDays,
            _recovered_from_run_journal: true,
          }},
          {{role: 'user', content: 'corrupt', timestamp: 5e12}},
          {{role: 'user', content: 'still today', timestamp: today}},
          {{
            role: 'assistant',
            content: '',
            reasoning: 'a thought from yesterday',
            timestamp: yesterday,
            _recovered_from_run_journal: true,
          }},
        ];
        process.stdout.write(JSON.stringify({{
          labels: labels(messages),
          kept: _messageDateSepDayKey({{
            role: 'assistant',
            content: [{{type: 'text', text: 'visible'}}],
            timestamp: yesterday,
            _recovered_from_run_journal: true,
          }}),
          imageOnly: _messageDateSepDayKey({{
            role: 'assistant',
            content: [{{type: 'image', text: 'not rendered as text'}}],
            timestamp: today,
            _recovered_from_run_journal: true,
          }}),
        }}));
        """
    )
    assert result["labels"] == ["Yesterday", "Today", "Yesterday"]
    assert result["kept"]
    assert result["imageOnly"] == ""


def test_render_loop_uses_the_date_separator_helper():
    assert "_nextDateSepText(m, _prevSepKey)" in UI_JS


def _payload_apply_prelude() -> str:
    """Real list-apply plus the clocks and day-key helpers it has to feed."""
    sessions_names = (
        "_serverNowMs",
        "_sessionTimestampMs",
        "_sessionSortTimestampMs",
        "_applySessionListPayload",
    )
    ui_names = (
        "_recoveredAnchorText",
        "_isEmptyRecoveredJournalAnchor",
        "_messageDateSepDayKey",
    )
    parts = [_extract_function(SESSIONS_JS, name) for name in sessions_names]
    parts.extend(_extract_function(UI_JS, name) for name in ui_names)
    return "\n".join(parts)


@pytest.mark.parametrize("skew_hours", [-72, 0, 72])
def test_payload_server_clock_keeps_recent_and_pending_timestamps(skew_hours):
    """A real /api/sessions payload must sync the +48h boundary to the server.

    Browser clocks 72h behind, level, and 72h ahead all used to be fine on
    master. The new cutoff compared against raw Date.now(), so a client 72h
    behind rejected a session the server had just stamped.
    """
    result = _run_node(
        f"""
        let _serverTimeDelta = 0;
        let _serverTz = '';
        let _otherProfileCount = 0;
        let _archivedWebuiCount = 0;
        let _archivedCliCount = 0;
        let _serverWebuiSessionCount = null;
        let _serverCliSessionCount = null;
        let _optimisticallyRemovedSessionIds = new Set();
        let _allSessions = [];
        let _sidebarReferenceSessions = [];
        let _allSessionsScope = null;
        let _allProjects = [];
        let _sessionListLoadError = null;
        let _sessionListHasLoadedOnce = false;
        let _sessionListFirstRenderAnimated = true;
        let _sessionListSkeletonActive = false;
        let _sessionListRefreshAnimationPending = false;
        let _lastSessionListRenderSig = null;
        let _activeProject = null;
        let _showAllProfiles = false;
        let _sessionSourceFilter = 'webui';
        let _renamingSid = null;
        let _sessionActionMenu = null;
        let _cronPollGeneration = 0;
        const S = {{ activeProfile: 'default' }};
        function _reconcileActiveSessionIdleStateFromList(rows) {{ return rows; }}
        function _mergeOptimisticFirstTurnSessions(rows) {{ return rows; }}
        function _requestedSessionSidebarSource() {{ return 'webui'; }}
        function _sessionListExcludeHiddenEnabled() {{ return false; }}
        function _recordSessionProfileCount() {{}}
        function _syncSessionAttentionSoundState() {{}}
        function _pruneLineageReportCacheToVisibleSessions() {{}}
        function _markPollingCompletionUnreadTransitions() {{}}
        function _isSessionEffectivelyStreaming() {{ return false; }}
        function startStreamingPoll() {{}}
        function stopStreamingPoll() {{}}
        function ensureSessionTimeRefreshPoll() {{}}
        function ensureActiveSessionExternalRefreshPoll() {{}}
        function ensureSessionEventsSSE() {{}}
        function animateNextSessionListRefresh() {{}}
        function _sessionListRenderSignature() {{ return 'sig'; }}
        function _purgeStaleInflightEntries() {{}}
        function _repaintSidebarForBackgroundChurn() {{}}
        function _repaintSidebarKeepingProjectPicker() {{}}
        {_payload_apply_prelude()}
        const serverNowSec = Math.floor(Date.now() / 1000);
        const serverNowMs = serverNowSec * 1000;
        const skewMs = {skew_hours} * 3600 * 1000;
        Date.now = () => serverNowMs + skewMs;
        const recent = serverNowSec - 120;
        const pending = recent + 40;
        const within48h = serverNowSec + 30 * 3600;
        const beyond = serverNowSec + 10 * 24 * 3600;
        const saneOld = 1776441972;
        const recentSession = {{
          session_id: 'recent',
          last_message_at: recent,
          updated_at: saneOld,
          pending_started_at: pending,
        }};
        const pendingAhead = {{
          session_id: 'pending-ahead',
          last_message_at: recent,
          updated_at: saneOld,
          pending_started_at: within48h,
        }};
        const fallbackSession = {{
          session_id: 'fallback',
          last_message_at: 5e12,
          updated_at: recent,
          pending_started_at: 5e12,
        }};
        const corruptSession = {{
          session_id: 'corrupt',
          last_message_at: beyond,
          updated_at: 4e9,
          created_at: -5,
          pending_started_at: beyond,
        }};
        const aheadOk = {{
          session_id: 'ahead-ok',
          last_message_at: within48h,
          updated_at: saneOld,
        }};
        _applySessionListPayload({{
          server_time: serverNowSec,
          server_tz: '+0000',
          sessions: [recentSession, pendingAhead, fallbackSession, corruptSession, aheadOk],
          other_profile_count: 0,
          archived_count: 0,
          active_profile: 'default',
          webui_session_count: 5,
          cli_session_count: 0,
        }}, {{ projects: [] }});
        process.stdout.write(JSON.stringify({{
          applied: _allSessions.length,
          delta: _serverTimeDelta,
          serverNow: _serverNowMs(),
          recent: _sessionTimestampMs(recentSession),
          recentSort: _sessionSortTimestampMs(recentSession),
          pendingAheadSort: _sessionSortTimestampMs(pendingAhead),
          fallback: _sessionTimestampMs(fallbackSession),
          fallbackSort: _sessionSortTimestampMs(fallbackSession),
          corrupt: _sessionTimestampMs(corruptSession),
          corruptSort: _sessionSortTimestampMs(corruptSession),
          ahead: _sessionTimestampMs(aheadOk),
          fixture: _sessionTimestampMs({{ last_message_at: 10, updated_at: 20 }}),
          dayKey: _messageDateSepDayKey({{ role: 'user', content: 'hi', timestamp: recent }}),
          dayKeyAhead: _messageDateSepDayKey({{ role: 'user', content: 'soon', timestamp: within48h }}),
          dayKeyBeyond: _messageDateSepDayKey({{ role: 'user', content: 'nope', timestamp: beyond }}),
          emptyAnchor: _messageDateSepDayKey({{
            role: 'assistant', content: '', timestamp: recent, _recovered_from_run_journal: true,
          }}),
          expectedDay: new Date(recent * 1000).toDateString(),
          expectedAheadDay: new Date(within48h * 1000).toDateString(),
          expectedRecent: recent * 1000,
          expectedPending: pending * 1000,
          expectedWithin: within48h * 1000,
          expectedDelta: skewMs,
          expectedServerNow: serverNowMs,
        }}));
        """
    )
    assert result["applied"] == 5
    assert result["delta"] == result["expectedDelta"] == skew_hours * 3600 * 1000
    assert result["serverNow"] == result["expectedServerNow"]
    assert result["recent"] == result["expectedRecent"]
    assert result["recentSort"] == result["expectedPending"]
    assert result["pendingAheadSort"] == result["expectedWithin"]
    assert result["ahead"] == result["expectedWithin"]
    assert result["fallback"] == result["expectedRecent"]
    assert result["fallbackSort"] == result["expectedRecent"]
    assert result["corrupt"] == 0
    assert result["corruptSort"] == 0
    assert result["fixture"] == 10_000
    assert result["dayKey"] == result["expectedDay"]
    assert result["dayKeyAhead"] == result["expectedAheadDay"]
    assert result["dayKeyBeyond"] == ""
    assert result["emptyAnchor"] == ""
