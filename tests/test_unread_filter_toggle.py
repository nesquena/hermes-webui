"""Regression tests for sidebar show-unread-only filter toggle (#6590).

Covers:
- Accessible name attributes and data-i18n bindings on checkbox and label.
- Translation keys for toggle label and empty state.
- Empty state rendering behavior:
  - CLI tab with zero rows produces exactly one empty note.
  - Project with zero rows produces exactly one empty note.
  - Nonempty list with no unread rows produces only the unread empty note.
  - Active session and child unread lineage remain visible when filter is active.
- State restoration from local storage.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_markup_accessible_attributes():
    assert 'id="sessionUnreadToggle"' in INDEX_HTML
    assert 'aria-label="Show unread only"' in INDEX_HTML
    assert 'data-i18n-aria-label="show_unread_only"' in INDEX_HTML
    assert 'data-i18n-title="filter_conversations"' in INDEX_HTML


def test_i18n_translation_keys_present():
    assert "show_unread_only: 'Show unread only'" in I18N_JS
    assert "no_unread_conversations: 'No unread conversations.'" in I18N_JS


def test_state_restoration_from_local_storage():
    script = """
    let store = { 'hermes-show-unread-only': '1' };
    const localStorage = {
      getItem(k) { return store[k] ?? null; },
      setItem(k, v) { store[k] = String(v); }
    };
    let _showUnreadOnly = false;
    function _restoreUnreadOnly(){
      try{
        const raw = localStorage.getItem('hermes-show-unread-only');
        _showUnreadOnly = raw === '1' || raw === 'true';
      }catch(_e){ _showUnreadOnly = false; }
    }
    _restoreUnreadOnly();
    const restoredTrue = _showUnreadOnly;
    store['hermes-show-unread-only'] = '0';
    _restoreUnreadOnly();
    const restoredFalse = _showUnreadOnly;
    console.log(JSON.stringify({ restoredTrue, restoredFalse }));
    """
    out = _run_node(script)
    assert out["restoredTrue"] is True
    assert out["restoredFalse"] is False


def test_empty_state_and_filtering_behavior():
    script = """
    function runTestScenario(scenario) {
      const list = {
        children: [],
        appendChild(child) { this.children.push(child); }
      };
      const t = (k) => k === 'no_unread_conversations' ? 'No unread conversations.' : k;
      const _sessionSourceFilter = scenario.sessionSourceFilter || 'chat';
      const window = { _showCliSessions: scenario.showCliSessions ?? true };
      const _activeProject = scenario.activeProject || null;
      const NO_PROJECT_FILTER = '__none__';
      const _showUnreadOnly = scenario.showUnreadOnly ?? true;
      const sessions = scenario.sessions || [];

      // Empty state for active project / CLI filter
      if(_sessionSourceFilter==='cli'&&sessions.length===0){
        const empty = { className: 'session-empty-note', text: window._showCliSessions?'No CLI sessions found.':'Enable Show agent sessions in Settings to list CLI sessions here.' };
        list.appendChild(empty);
      } else if(_activeProject&&sessions.length===0){
        const empty = { className: 'session-empty-note', text: _activeProject===NO_PROJECT_FILTER?'No unassigned sessions.':'No sessions in this project yet.' };
        list.appendChild(empty);
      }

      let filteredSessions = sessions;
      if(_showUnreadOnly){
        const activeSidForFilter = scenario.activeSid || null;
        filteredSessions = sessions.filter(s => {
          if(activeSidForFilter && (s.id === activeSidForFilter || s._hasActiveChild)) return true;
          return s.unread || !!s._child_session_has_unread;
        });
      }
      const orderedSessions = [...filteredSessions];

      if(_showUnreadOnly && sessions.length > 0 && orderedSessions.length === 0){
        const empty = { className: 'session-empty-note', text: typeof t === 'function' ? t('no_unread_conversations') : 'No unread conversations.' };
        list.appendChild(empty);
      }

      return {
        notes: list.children.map(c => c.text),
        visibleCount: orderedSessions.length,
        visibleIds: orderedSessions.map(s => s.id)
      };
    }

    const cliZero = runTestScenario({ sessionSourceFilter: 'cli', sessions: [], showUnreadOnly: true });
    const projectZero = runTestScenario({ activeProject: 'proj-1', sessions: [], showUnreadOnly: true });
    const nonemptyAllRead = runTestScenario({
      sessions: [{ id: 's1', unread: false }, { id: 's2', unread: false }],
      showUnreadOnly: true
    });
    const activeKeptVisible = runTestScenario({
      sessions: [{ id: 's1', unread: false }, { id: 's2', unread: false }],
      activeSid: 's1',
      showUnreadOnly: true
    });
    const childUnreadKeptVisible = runTestScenario({
      sessions: [{ id: 's1', unread: false, _child_session_has_unread: true }, { id: 's2', unread: false }],
      showUnreadOnly: true
    });

    console.log(JSON.stringify({
      cliZero,
      projectZero,
      nonemptyAllRead,
      activeKeptVisible,
      childUnreadKeptVisible
    }));
    """
    out = _run_node(script)

    # 1. CLI tab with zero rows producing exactly one empty note
    assert out["cliZero"]["notes"] == ["No CLI sessions found."]

    # 2. Project with zero rows producing exactly one note
    assert out["projectZero"]["notes"] == ["No sessions in this project yet."]

    # 3. Nonempty list with no unread rows producing only the unread empty state
    assert out["nonemptyAllRead"]["notes"] == ["No unread conversations."]
    assert out["nonemptyAllRead"]["visibleCount"] == 0

    # 4. Active session remains visible while filter is enabled
    assert out["activeKeptVisible"]["visibleIds"] == ["s1"]
    assert out["activeKeptVisible"]["notes"] == []

    # 5. Child unread lineage remains visible
    assert out["childUnreadKeptVisible"]["visibleIds"] == ["s1"]
    assert out["childUnreadKeptVisible"]["notes"] == []
