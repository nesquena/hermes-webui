"""Regression coverage for gateway SSE reconnect refresh dedupe."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
GATEWAY_WATCHER = ROOT / "api" / "gateway_watcher.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def test_gateway_watcher_remains_hash_only():
    """The watcher should not try to infer restarts from state.db mtime."""
    src = _read(GATEWAY_WATCHER)
    poll = _block(src, "    def _poll_loop(self):", "\n_watchers:")

    assert "_get_db_mtime" not in src
    assert "_detect_gateway_restart" not in src
    assert "current_hash = _snapshot_hash(sessions)" in poll
    assert "if current_hash != self._last_hash:" in poll
    assert "_notify_subscribers(sessions)" in poll


def test_gateway_sse_dedupes_reconnect_snapshot_before_refresh():
    """Reconnect initial snapshots should not force a sidebar refetch."""
    src = _read(SESSIONS_JS)
    handler = _block(
        src,
        "_gatewaySSE.addEventListener('sessions_changed'",
        "_gatewaySSE.onerror",
    )

    assert "function _gatewaySessionSnapshotKey" in src
    assert "function _isDuplicateGatewaySessionSnapshot" in src
    assert "if(!_isDuplicateGatewaySessionSnapshot(data.sessions))" in handler
    assert "renderSessionList({deferWhileInteracting:true}); // re-fetch and re-render" in handler


def test_gateway_probe_reattaches_sse_after_profile_switch_restart():
    """A healthy probe must revive the EventSource when the watcher restarted."""
    src = _read(SESSIONS_JS)
    probe = _block(src, "async function probeGatewaySSEStatus()", "\n\nfunction startGatewaySSE")

    assert "if(!_gatewaySSE && typeof EventSource!=='undefined' && !(document&&document.hidden)) startGatewaySSE();" in probe


def test_gateway_snapshot_key_matches_backend_hash_fields():
    """Frontend dedupe must compare the same fields that drive watcher events."""
    src = _read(SESSIONS_JS)
    key_fn = _block(
        src,
        "function _gatewaySessionSnapshotKey",
        "\n\nfunction _isGatewaySessionForSnapshot",
    )

    assert "s.session_id" in key_fn
    assert "s.updated_at||0" in key_fn
    assert "s.message_count||0" in key_fn
    assert ".sort()" in key_fn


def test_gateway_snapshot_dedupe_logic_filters_symmetrically():
    """Exercise the dedupe helpers, including null and webui noise."""
    script = r"""
function _isCliSession(session) {
  return session && (session.session_source === 'cli' || session.raw_source === 'cli' || session.is_cli_session === true);
}
function _isMessagingSession(session) {
  return session && session.session_source === 'messaging';
}
function _gatewaySessionSnapshotKey(sessions){
  return (Array.isArray(sessions)?sessions:[])
    .filter(s=>s&&s.session_id)
    .map(s=>`${s.session_id}:${s.updated_at||0}:${s.message_count||0}`)
    .sort()
    .join('|');
}
function _isGatewaySessionForSnapshot(session){
  if(!session) return false;
  if(typeof _isCliSession==='function'&&_isCliSession(session)) return true;
  if(typeof _isMessagingSession==='function'&&_isMessagingSession(session)) return true;
  const source=String(session.session_source||session.raw_source||session.source_tag||session.source||'').toLowerCase();
  return !!source&&source!=='webui';
}
function _isDuplicateGatewaySessionSnapshot(sessions){
  const incoming=(Array.isArray(sessions)?sessions:[]).filter(_isGatewaySessionForSnapshot);
  const currentGatewaySessions=(Array.isArray(globalThis._allSessions)?globalThis._allSessions:[]).filter(_isGatewaySessionForSnapshot);
  if(!incoming.length&&!currentGatewaySessions.length) return true;
  return _gatewaySessionSnapshotKey(incoming)===_gatewaySessionSnapshotKey(currentGatewaySessions);
}

globalThis._allSessions = [
  {session_id:'cli-1', updated_at:10, message_count:2, session_source:'cli'},
  {session_id:'msg-1', updated_at:20, message_count:5, session_source:'messaging'},
  {session_id:'web-1', updated_at:30, message_count:1, session_source:'webui'},
  null,
];

const duplicateWithNoise = [
  null,
  {session_id:'web-2', updated_at:99, message_count:1, session_source:'webui'},
  {session_id:'msg-1', updated_at:20, message_count:5, session_source:'messaging'},
  {session_id:'cli-1', updated_at:10, message_count:2, session_source:'cli'},
];
if(!_isDuplicateGatewaySessionSnapshot(duplicateWithNoise)) throw new Error('expected duplicate snapshot');

const changed = [
  {session_id:'cli-1', updated_at:10, message_count:3, session_source:'cli'},
  {session_id:'msg-1', updated_at:20, message_count:5, session_source:'messaging'},
];
if(_isDuplicateGatewaySessionSnapshot(changed)) throw new Error('expected changed snapshot');

globalThis._allSessions = [{session_id:'web-1', updated_at:1, message_count:1, session_source:'webui'}];
if(!_isDuplicateGatewaySessionSnapshot([null, {session_id:'web-2', session_source:'webui'}])) throw new Error('expected empty gateway snapshot duplicate');
"""
    subprocess.run(["node", "-e", script], check=True)


def test_load_session_persists_only_after_metadata_loads():
    """Do not overwrite the last good localStorage sid before /api/session succeeds."""
    src = _read(SESSIONS_JS)
    load = _block(src, "async function loadSession(sid)", "activeStreamId=S.session.active_stream_id")
    api_pos = load.index("data = await api(`/api/session")
    persist_pos = load.index("localStorage.setItem('hermes-webui-session',S.session.session_id)")

    assert "_persistActiveSession" not in src
    assert persist_pos > api_pos
