import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _extract_js_function(source, name):
    marker = f"function {name}"
    start = source.index(marker)
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    opening = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced JavaScript function: {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_delete_cleanup_flags_are_observable_in_single_and_batch_flows():
    src = read("static/sessions.js")
    delete_source = _extract_js_function(src, "deleteSession")
    batch_source = _extract_js_function(src, "_renderBatchActionBar")
    harness = (
        "const deleteSession = eval('(' + " + json.dumps(delete_source) + " + ')');\n"
        "const renderBatchActionBar = eval('(' + " + json.dumps(batch_source) + " + ')');\n"
        "const toasts = [];\n"
        "const bar = { children: [], style: {}, appendChild(node) { this.children.push(node); }, "
        "querySelectorAll() { return []; } };\n"
        "const localStorage = { removeItem() {} };\n"
        "const S = { session: null, messages: [], entries: [] };\n"
        "const _allSessions = [];\n"
        "const _optimisticallyRemovedSessionIds = new Set();\n"
        "let _pendingSessionReflowPositions = null;\n"
        "const _selectedSessions = new Set();\n"
        "const _sessionSelectMode = true;\n"
        "const _allProjects = [];\n"
        "const document = { createElement() { return { children: [], style: {}, "
        "appendChild(node) { this.children.push(node); } }; } };\n"
        "function $(id) { return id === 'batchActionBar' ? bar : { style: {}, innerHTML: '' }; }\n"
        "function t(key) { return key; }\n"
        "async function showConfirmDialog() { return true; }\n"
        "function _sessionSnapshotById() { return null; }\n"
        "function _captureSessionReflowPositions() { return null; }\n"
        "function _clearHandoffStorageForSession() {}\n"
        "function _optimisticallyRemoveSessionFromList() {}\n"
        "function renderSessionListFromCache() {}\n"
        "async function renderSessionList() {}\n"
        "function _clearPersistedSessionQueue() {}\n"
        "function _sessionResponseRetainsWorktree() { return false; }\n"
        "function _worktreeSessionCount() { return 0; }\n"
        "function _worktreeResponseCount() { return 0; }\n"
        "function exitSessionSelectMode() {}\n"
        "function showToast(message) { toasts.push(message); }\n"
        "function setStatus() {}\n"
        "function api(path) { return Promise.resolve({ state_db_cleanup_failed: false, "
        "run_journal_cleanup_failed: true }); }\n"
        "async function run() {\n"
        "  const singleResult = await deleteSession('single');\n"
        "  _selectedSessions.add('batch');\n"
        "  renderBatchActionBar();\n"
        "  const deleteButton = bar.children.find(node => node.textContent === 'session_batch_delete');\n"
        "  await deleteButton.onclick();\n"
        "  process.stdout.write(JSON.stringify({ singleResult, toasts }));\n"
        "}\n"
        "run().catch(error => { console.error(error); process.exit(1); });\n"
    )
    result = subprocess.run(
        [shutil.which("node"), "-e", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["singleResult"] is False
    assert payload["toasts"] == ["delete_failed", "delete_failed (1/1)"]


def test_delete_confirmation_mentions_retained_worktree():
    src = read("static/sessions.js")
    i18n = read("static/i18n.js")
    assert "function _sessionSnapshotById(sid)" in src
    assert "session.worktree_path?t('session_delete_worktree_confirm',session.worktree_path)" in src
    assert "session_delete_worktree_confirm" in i18n
    assert "will remain on disk" in i18n
    assert "session_delete_worktree_confirm: (path) => `Delete this conversation? The worktree at ${path} will remain on disk.`" in i18n
    assert "session_delete_worktree_desc: 'Delete only the WebUI conversation; keep the worktree on disk'" in i18n
    assert "session_deleted_worktree: 'Conversation deleted. Worktree remains on disk.'" in i18n


def test_batch_archive_delete_confirmations_count_worktree_sessions():
    src = read("static/sessions.js")
    i18n = read("static/i18n.js")
    assert "function _worktreeSessionCount(ids)" in src
    assert "function _worktreeResponseCount(results)" in src
    assert "session_batch_delete_worktree_confirm" in src
    assert "session_batch_archive_worktree_confirm" in src
    assert "session_batch_delete_worktree_confirm" in i18n
    assert "session_batch_archive_worktree_confirm" in i18n


def test_archive_and_delete_action_descriptions_are_worktree_specific():
    src = read("static/sessions.js")
    i18n = read("static/i18n.js")
    assert "function _sessionArchiveDescription(session)" in src
    assert "function _sessionDeleteDescription(session)" in src
    assert "session&&session.worktree_path?t('session_archive_worktree_desc')" in src
    assert "session&&session.worktree_path?t('session_delete_worktree_desc')" in src
    assert "session_archive_worktree_desc" in i18n
    assert "session_delete_worktree_desc" in i18n
    assert "session_archive_worktree_desc: 'Hide this conversation; keep its worktree on disk'" in i18n
    assert "session_archived_worktree: 'Session archived. Worktree remains on disk.'" in i18n


def test_archive_delete_success_copy_prefers_response_worktree_retained():
    src = read("static/sessions.js")
    assert "function _sessionResponseRetainsWorktree(response, session)" in src
    assert "typeof response.worktree_retained==='boolean'" in src
    assert "return response.worktree_retained;" in src
    assert "return !!(session&&session.worktree_path);" in src
    assert src.index("return response.worktree_retained;") < src.index(
        "return !!(session&&session.worktree_path);"
    )
    assert "function _sessionArchiveToast(response, session)" in src
    assert "session.archived?_sessionArchiveToast(response,session):t('session_restored')" in src
    assert "_sessionResponseRetainsWorktree(response,session)?t('session_deleted_worktree')" in src
    assert "const retainedCount=_worktreeResponseCount(results)" in src
    assert "const cleanupFailedCount=results.filter(result=>result.response&&(result.response.state_db_cleanup_failed||result.response.run_journal_cleanup_failed)).length;" in src
    assert "if(cleanupFailedCount) showToast(t('delete_failed')+' ('+cleanupFailedCount+'/'+ids.length+')',0,'error');" in src
    assert "showToast(retainedCount?t('session_archived_worktree'):t('session_archived'))" in src
    assert "showToast((retainedCount?t('session_deleted_worktree'):t('session_delete'))" in src
    assert "const cleanupFailed=!!(response&&(response.state_db_cleanup_failed||response.run_journal_cleanup_failed));" in src
    assert "if(cleanupFailed) showToast(t('delete_failed'),0,'error');" in src
    assert "return !cleanupFailed;" in src


def test_worktree_archive_delete_api_responses_are_explicit():
    src = read("api/routes.py")
    assert "def _worktree_retained_payload(session)" in src
    assert "def _worktree_retained_payload_for_session_id(sid: str)" in src
    assert '"worktree_retained": True' in src
    assert '"state_db_cleanup_failed": state_db_cleanup_failed' in src
    assert '"run_journal_cleanup_failed": run_journal_cleanup_failed' in src
    assert '"ok": True,' in src
    assert "**worktree_retained," in src
    assert '{"ok": True, "session": s.compact(), **_worktree_retained_payload(s)}' in src


def test_remove_worktree_ui_does_not_force_unsafe_status_by_default():
    src = read("static/sessions.js")
    i18n = read("static/i18n.js")
    assert "async function removeWorktree(session)" in src
    assert "status.dirty||status.untracked_count>0||(status.ahead_behind&&status.ahead_behind.ahead>0)" in src
    assert "session_worktree_remove_unsafe_blocked" in src
    assert "session_worktree_remove_unsafe_blocked" in i18n
    assert "Resolve local changes or unpushed commits before removing this worktree." in i18n
    assert "JSON.stringify({session_id:session.session_id, force:false})" in src
    assert "const force=(status.dirty||status.untracked_count>0)" not in src
