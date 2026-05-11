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


def test_worktree_archive_delete_api_responses_are_explicit():
    src = read("api/routes.py")
    assert "def _worktree_retained_payload(session)" in src
    assert "def _worktree_retained_payload_for_session_id(sid: str)" in src
    assert '"worktree_retained": True' in src
    assert '{"ok": True, **worktree_retained}' in src
    assert '{"ok": True, "session": s.compact(), **_worktree_retained_payload(s)}' in src
