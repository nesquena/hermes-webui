"""A stale WebUI sidecar must not hide a durable compression continuation."""
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes, profiles


@pytest.fixture
def lineage(tmp_path, monkeypatch):
    state = pytest.importorskip("hermes_state")
    # Worker tests restore sys.path after loading Agent lazily. Scope its
    # sibling-module lookup to this fixture, never the global test importer.
    monkeypatch.syspath_prepend(str(Path(state.__file__).parent))
    SessionDB = state.SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sealedparent", source="webui")
    db.create_session("idlechild", source="tui", parent_session_id="sealedparent")
    db.append_message("idlechild", "user", "retained task")
    db.end_session("sealedparent", "compression")
    db.end_session("idlechild", "idle_timeout")
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda name: str(tmp_path))
    session = SimpleNamespace(session_id="sealedparent", profile="default", pre_compression_snapshot=False)
    yield db, session
    db.close()


def test_reload_resolves_sqlite_rotation_without_sidecar_flag(lineage):
    db, session = lineage
    before = db.get_session("idlechild")["ended_at"]
    assert routes._pre_compression_continuation_session_id(session) == "idlechild"
    assert db.get_session("idlechild")["ended_at"] == before
    assert db.get_session("sealedparent")["end_reason"] == "compression"


class Handler:
    headers = {}
    def __init__(self):
        self.wfile = io.BytesIO()
    def send_response(self, status):
        self.status = status
    def send_header(self, *args):
        pass
    def end_headers(self):
        pass


def _assert_stale_post_rotation(session, monkeypatch, expected_continuation):
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *a, **kw: session)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a: True)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *a: pytest.fail("stale turn must not reach workspace mutation"))
    h = Handler()
    routes._handle_chat_start(h, {"session_id": session.session_id, "message": "conclusion?"})
    assert h.status == 409
    payload = json.loads(h.wfile.getvalue())
    assert payload["code"] == "session_rotated"
    assert payload["continuation_session_id"] == expected_continuation


def test_stale_post_rejected_before_workspace_or_worker_mutation(lineage, monkeypatch):
    _, session = lineage
    _assert_stale_post_rotation(session, monkeypatch, "idlechild")


@pytest.mark.parametrize("source", ["desktop", "acp"])
def test_same_source_local_interactive_lineage_resumes(lineage, monkeypatch, source):
    db, session = lineage
    db._conn.execute(
        "UPDATE sessions SET source=? WHERE id IN ('sealedparent', 'idlechild')",
        (source,),
    )
    db._conn.commit()

    assert routes._pre_compression_continuation_session_id(session) == "idlechild"
    _assert_stale_post_rotation(session, monkeypatch, "idlechild")


@pytest.mark.parametrize(
    "source",
    ["cron", "webhook", "kanban", "tool", "subagent", "unknown-source"],
)
def test_noninteractive_lineage_is_not_resumable(lineage, monkeypatch, source):
    db, session = lineage
    db._conn.execute(
        "UPDATE sessions SET source=? WHERE id IN ('sealedparent', 'idlechild')",
        (source,),
    )
    db._conn.commit()

    assert routes._pre_compression_continuation_session_id(session) is None
    _assert_stale_post_rotation(session, monkeypatch, None)


def test_idle_tip_accepts_normal_persistence_without_reopening_parent(lineage):
    db, session = lineage
    tip = routes._pre_compression_continuation_session_id(session)
    db.append_message(tip, "user", "resume task")
    assert db.get_session("sealedparent")["end_reason"] == "compression"
    assert db.get_messages(tip)[-1]["content"] == "resume task"


@pytest.mark.parametrize("reason", ["reset", "user_closed", "compression"])
@pytest.mark.parametrize("ended_at", [None, 1234.0])
def test_unknown_or_sealed_terminal_tip_is_not_redirected(lineage, reason, ended_at):
    db, session = lineage
    db._conn.execute("UPDATE sessions SET end_reason=?, ended_at=? WHERE id='idlechild'", (reason, ended_at))
    db._conn.commit()
    assert routes._pre_compression_continuation_session_id(session) is None


def test_newer_branch_does_not_replace_canonical_continuation(lineage):
    db, session = lineage
    db.create_session("branchchild", source="webui", parent_session_id="sealedparent")
    db._conn.execute("UPDATE sessions SET message_count=20, model_config=? WHERE id='branchchild'", (json.dumps({'_branched_from': 'sealedparent'}),))
    db._conn.commit()
    assert routes._pre_compression_continuation_session_id(session) == "idlechild"


def test_foreign_profile_and_subagent_are_not_redirected(lineage):
    db, session = lineage
    db._conn.execute("UPDATE sessions SET profile_name='foreign' WHERE id='idlechild'")
    db._conn.commit()
    assert routes._pre_compression_continuation_session_id(session) is None
    db._conn.execute("UPDATE sessions SET profile_name=NULL, source='subagent' WHERE id='idlechild'")
    db._conn.commit()
    assert routes._pre_compression_continuation_session_id(session) is None


def test_noncompressed_parent_and_absent_database_keep_legacy_behavior(lineage, monkeypatch, tmp_path):
    db, session = lineage
    db._conn.execute("UPDATE sessions SET end_reason='idle_timeout' WHERE id='sealedparent'")
    db._conn.commit()
    assert routes._pre_compression_continuation_session_id(session) is None
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda name: str(tmp_path / 'missing'))
    assert routes._pre_compression_continuation_session_id(session) is None
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('method', ['get_session', 'get_compression_tip'])
@pytest.mark.parametrize('kind', ['missing', 'noncallable', 'signature'])
def test_older_agent_keeps_sidecar_recovery(tmp_path, monkeypatch, method, kind):
    import sys
    from api.compression_continuation import durable_compression_continuation

    class OldDB:
        closed = False
        def __init__(self, *args, **kwargs):
            pass
        def get_session(self, sid):
            return {'end_reason': 'compression'}
        def get_compression_tip(self, sid):
            return 'legacychild'
        def close(self):
            OldDB.closed = True

    if kind == 'missing':
        delattr(OldDB, method)
    elif kind == 'noncallable':
        setattr(OldDB, method, None)
    else:
        setattr(OldDB, method, lambda self, sid, required: None)
    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=OldDB))
    (tmp_path / 'state.db').touch()
    monkeypatch.setattr(profiles, '_resolve_profile_home_for_name', lambda _: str(tmp_path))
    session = SimpleNamespace(session_id='legacyparent', profile='default', pre_compression_snapshot=True)
    child = SimpleNamespace(session_id='legacychild', profile='default',
                            parent_session_id='legacyparent', pre_compression_snapshot=False,
                            updated_at=2, created_at=1)
    monkeypatch.setattr(routes, 'SESSIONS', {'legacychild': child})
    monkeypatch.setattr(routes, 'SESSION_DIR', tmp_path)
    assert durable_compression_continuation(session) == (False, None)
    assert OldDB.closed
    assert routes._pre_compression_continuation_session_id(session) == 'legacychild'


def test_browser_rotation_restores_draft_without_reposting():
    source = (Path(__file__).parents[1] / 'static/messages.js').read_text()
    helper = source[source.index('async function _recoverCompressedSend('):source.index('function _restoreComposerDraftAfterFailedSend(')]
    script = r'''
const assert = require('node:assert/strict');
let S={session:{session_id:'old'}}, INFLIGHT={old:{}};
const calls=[];
const stopApprovalPolling=()=>{},stopClarifyPolling=()=>{},removeThinking=()=>{},setBusy=()=>{},setComposerStatus=()=>{},showToast=()=>{};
// Attachment semantics are covered by the composed #6304 harness.
const _releaseReplacedLiveAttachment=sid=>calls.push(['release',sid]);
const loadSession=async sid=>{calls.push(['load',sid]);S.session={session_id:sid}};
const _restoreComposerDraftAfterFailedSend=(...args)=>calls.push(['restore',...args]);
const api=()=>{throw Error('must not repost')};
(async()=>{
const err={status:409,body:JSON.stringify({code:'session_rotated',continuation_session_id:'new'})};
const files=[{name:'drawing.png'}], promise=Promise.resolve();
assert.equal(await _recoverCompressedSend(err,'old','conclusion?',files,promise),true);
assert.deepEqual(calls[0],['release','old']);
assert.deepEqual(calls[1],['load','new']);
assert.deepEqual(calls[2],['restore','conclusion?',files,'new',promise]);
assert.equal(INFLIGHT.old,undefined);
assert.equal(await _recoverCompressedSend({status:500},'new','x',[],promise),false);
assert.equal(await _recoverCompressedSend(err,'old','x',[],promise),false);
assert.equal(calls.length,3);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-e', helper + script], check=True, timeout=15)


def test_background_failed_draft_preserves_attachments_after_navigation_race():
    source = (Path(__file__).parents[1] / 'static/messages.js').read_text()
    helper = source[source.index('function _restoreComposerDraftAfterFailedSend('):source.index('async function send(){')]
    script = r'''
const assert=require('node:assert/strict');
const S={session:{session_id:'unrelated'},pendingFiles:[]};
const $=()=>{throw Error('unrelated visible composer must not be touched')};
const calls=[];
const _saveComposerDraftNow=(...args)=>calls.push(args);
(async()=>{
const files=[{name:'drawing.png',path:'/uploads/drawing.png'}];
_restoreComposerDraftAfterFailedSend('retained',files,'continuation',Promise.resolve());
await Promise.resolve();
assert.deepEqual(calls,[['continuation','retained',files]]);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node','-e',helper+script], check=True, timeout=15)
