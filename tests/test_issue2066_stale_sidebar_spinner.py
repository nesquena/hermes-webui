"""Regression checks for #2066 stale sidebar spinner state."""

import json
import subprocess
from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text()


def _function_block(name: str, next_name: str) -> str:
    start = SESSIONS_JS.find(f"function {name}")
    assert start != -1, f"{name} not found in sessions.js"
    end = SESSIONS_JS.find(f"function {next_name}", start)
    assert end != -1, f"{next_name} not found after {name}"
    return SESSIONS_JS[start:end]


def test_local_streaming_only_uses_active_session_busy_state():
    block = _function_block("_isSessionLocallyStreaming", "_isSessionEffectivelyStreaming")

    assert "const isActive = S.session && s.session_id === S.session.session_id;" in block
    assert "return isActive && Boolean(S.busy);" in block
    assert "INFLIGHT[s.session_id]" not in block
    assert "INFLIGHT && INFLIGHT[s.session_id]" not in block


def test_cache_render_purges_stale_non_streaming_inflight_entries():
    purge_block = _function_block("_purgeStaleInflightEntries", "_rememberRenderedStreamingState")
    render_block = _function_block("renderSessionListFromCache", "_showProjectPicker")

    assert "const sessionsById = new Map();" in purge_block
    assert "if (s && s.session_id) sessionsById.set(s.session_id, s);" in purge_block
    assert "const s = sessionsById.get(sid);" in purge_block
    assert "_allSessionsById" not in purge_block
    # Non-streaming sessions that ARE in _allSessions are purged (original #2066
    # semantics).  Sessions absent from _allSessions are also purged (adds #2092
    # ghost-entry cleanup); the guard check for !sessionsById.has(sid) must come
    # before the non-streaming check for code clarity and correctness.
    assert "if (!sessionsById.has(sid))" in purge_block
    assert "!s.is_streaming" in purge_block
    assert "delete INFLIGHT[sid];" in purge_block
    assert "clearInflightState(sid);" in purge_block
    assert "_purgeStaleInflightEntries();" in render_block


def test_stale_inflight_purge_executes_without_undeclared_session_map():
    purge_block = _function_block("_purgeStaleInflightEntries", "_rememberRenderedStreamingState")
    script = f"""
let _allSessions = [
  {{session_id: 'done-session', is_streaming: false}},
  {{session_id: 'running-session', is_streaming: true}}
];
let INFLIGHT = {{
  'done-session': true,
  'running-session': true,
  'unknown-session': true
}};
let cleared = [];
function clearInflightState(sid) {{
  cleared.push(sid);
}}
{purge_block}
_purgeStaleInflightEntries();
console.log(JSON.stringify({{inflight: INFLIGHT, cleared}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    # With #2092, sessions absent from _allSessions (like `unknown-session`)
    # are also purged and have clearInflightState called for them.  `done-session`
    # remains in _allSessions with is_streaming=false so it is still purged too.
    assert payload == {
        "inflight": {
            "running-session": True,
        },
        "cleared": sorted(["unknown-session", "done-session"]),
    }
