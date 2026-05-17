from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


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
    assert "showToast(retainedCount?t('session_archived_worktree'):t('session_archived'))" in src
    assert "showToast((retainedCount?t('session_deleted_worktree'):t('session_delete'))" in src


def test_worktree_archive_delete_api_responses_are_explicit():
    src = read("api/routes.py")
    assert "def _worktree_retained_payload(session)" in src
    assert "def _worktree_retained_payload_for_session_id(sid: str)" in src
    assert '"worktree_retained": True' in src
    assert '{"ok": True, **worktree_retained}' in src
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
