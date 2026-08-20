"""Coverage for api/session_squash.py — the in-process squash behind the
WebUI squash button (POST /api/session/squash + GET .../squash/status).

The flow is exercised end-to-end with a fake Session object and a real
sidecar file in a temp dir: job start, guard refusals, archive + manifest,
sidecar mutation contract (single visible summary + compaction marker +
manual anchor + watermark barrier + lineage detach), and job status.
"""

import gzip
import hashlib
import json
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import api.models
import api.routes
import api.session_ops
from api import session_squash


SID = "20260801_120000_ab12cd"


def _make_session(tmp_path, *, messages=None, **overrides):
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{SID}.json"
    if messages is None:
        messages = [
            {"role": "user", "content": "première demande de test", "timestamp": 1000.0},
            {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 fait", "timestamp": 1001.0},
            {"role": "user", "content": "seconde demande", "timestamp": 1002.0},
            {"role": "assistant", "content": "réponse finale", "timestamp": 1003.0},
        ]
    sess = SimpleNamespace(
        session_id=SID,
        title="session de test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=1003.0,
        messages=messages,
        context_messages=list(messages),
        tool_calls=[{"id": "t1"}],
        active_stream_id=None,
        active_checkpoint=None,
        pending_turn_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        parent_session_id="parent-fork-123",
        anchor_activity_scenes={"s1": {"foo": "bar"}},
        compression_anchor_visible_idx=None,
        compression_anchor_message_key=None,
        compression_anchor_summary=None,
        compression_anchor_mode=None,
        truncation_watermark=None,
        truncation_boundary=None,
        read_only=False,
        profile="default",
        path=path,
    )
    for key, value in overrides.items():
        setattr(sess, key, value)

    def _save(**_kwargs):
        payload = {
            "session_id": SID,
            "title": sess.title,
            "workspace": sess.workspace,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "messages": sess.messages,
            "context_messages": sess.context_messages,
            "tool_calls": sess.tool_calls,
            "active_stream_id": sess.active_stream_id,
            "active_checkpoint": sess.active_checkpoint,
            "pending_turn_id": sess.pending_turn_id,
            "pending_user_message": sess.pending_user_message,
            "pending_attachments": sess.pending_attachments,
            "pending_started_at": sess.pending_started_at,
            "pending_user_source": sess.pending_user_source,
            "parent_session_id": sess.parent_session_id,
            "anchor_activity_scenes": sess.anchor_activity_scenes,
            "compression_anchor_visible_idx": sess.compression_anchor_visible_idx,
            "compression_anchor_message_key": sess.compression_anchor_message_key,
            "compression_anchor_summary": sess.compression_anchor_summary,
            "compression_anchor_mode": sess.compression_anchor_mode,
            "truncation_watermark": sess.truncation_watermark,
            "truncation_boundary": sess.truncation_boundary,
            "message_count": len(sess.messages or []),
            "read_only": sess.read_only,
            "profile": sess.profile,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sess.save = _save
    _save()
    return sess


def _run_job(sess, *, summary="synthèse fournie " * 40):
    summary = summary.strip()
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess), \
         patch.object(api.session_ops, "_live_active_stream_id", lambda _s: None), \
         patch.object(api.routes, "_get_session_agent_lock", _dummy_lock), \
         patch.object(api.routes, "_publish_session_list_changed", lambda *a, **k: None), \
         patch("api.config._evict_session_agent", lambda _sid: None), \
         patch.object(session_squash, "_generate_summary", lambda s, sid, provided: (provided, "provided")):
        job = session_squash.start_squash_job(SID, confirm_session_id=SID, summary=summary)
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = session_squash.squash_job_status(job["job_id"])
            if snap["status"] in ("done", "error"):
                return snap
            time.sleep(0.02)
    raise AssertionError("squash job did not finish")


class _DummyLock:
    def acquire(self, timeout=None):
        return True

    def release(self):
        return None


def _dummy_lock(_sid):
    return _DummyLock()


def test_squash_job_collapses_session(tmp_path):
    sess = _make_session(tmp_path)
    original_bytes = sess.path.read_bytes()
    original_sha = hashlib.sha256(original_bytes).hexdigest()

    snap = _run_job(sess)
    assert snap["status"] == "done", snap.get("error")
    result = snap["result"]
    assert result["already_squashed"] is False
    assert result["before"]["message_count"] == 4
    assert result["after"]["message_count"] == 1
    assert result["original_sha256"] == original_sha

    persisted = json.loads(sess.path.read_text(encoding="utf-8"))
    assert len(persisted["messages"]) == 1
    assert persisted["messages"][0]["_squash_summary"] is True
    assert persisted["messages"][0]["role"] == "assistant"
    assert len(persisted["context_messages"]) == 1
    assert persisted["context_messages"][0]["content"].startswith("[CONTEXT COMPACTION")
    assert persisted["compression_anchor_mode"] == "manual"
    assert persisted["compression_anchor_visible_idx"] == 0
    assert persisted["compression_anchor_message_key"]["role"] == "assistant"
    assert persisted["truncation_watermark"] == persisted["truncation_boundary"]
    assert persisted["truncation_watermark"] > 1003.0
    assert persisted["parent_session_id"] is None
    assert persisted["active_stream_id"] is None
    assert persisted["pending_user_message"] is None
    assert persisted["tool_calls"] == []

    # Archive: gzip payload matches the original checksum, manifest is
    # compatible with the squash-chat skill's restore command.
    archive_path = sess.path.parent.parent / "session-squash-archives" / SID / result["archive_path"].split("/")[-1]
    assert archive_path.is_file()
    with gzip.open(archive_path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == original_sha
    manifest = json.loads(
        archive_path.with_suffix(archive_path.suffix + ".manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["session_id"] == SID
    assert manifest["source_sha256"] == original_sha

    # No stale .bak may survive (startup recovery could undo the squash).
    assert not sess.path.with_suffix(".json.bak").exists()


def test_squash_requires_confirm(tmp_path):
    _make_session(tmp_path)
    with pytest.raises(session_squash.SquashError):
        session_squash.start_squash_job(SID, confirm_session_id="wrong", summary=None)


def test_squash_refuses_active_session(tmp_path):
    sess = _make_session(tmp_path, active_stream_id="stream-123")
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess):
        with pytest.raises(session_squash.SquashError) as excinfo:
            session_squash.start_squash_job(SID, confirm_session_id=SID, summary=None)
    assert excinfo.value.status == 409


def test_squash_refuses_read_only(tmp_path):
    sess = _make_session(tmp_path, read_only=True)
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess):
        with pytest.raises(session_squash.SquashError):
            session_squash.start_squash_job(SID, confirm_session_id=SID, summary=None)


def test_squash_refuses_empty_session(tmp_path):
    sess = _make_session(tmp_path, messages=[])
    snap = _run_job(sess)
    assert snap["status"] == "error"
    assert "nothing to squash" in snap["error"]


def test_squash_already_squashed_is_idempotent(tmp_path):
    sess = _make_session(tmp_path, messages=[{
        "role": "assistant", "content": "synthèse", "timestamp": 1.0, "_squash_summary": True,
    }])
    snap = _run_job(sess)
    assert snap["status"] == "done"
    assert snap["result"]["already_squashed"] is True


def test_distill_transcript_respects_budget():
    sess = SimpleNamespace(messages=[
        {"role": "user", "content": "demande " * 500, "timestamp": float(i)}
        for i in range(40)
    ])
    distilled = session_squash._distill_transcript(sess, budget=5000)
    assert len(distilled) <= 5000
    assert "demande" in distilled


def test_fallback_summary_has_all_sections():
    sess = SimpleNamespace(
        title="titre test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=2000.0,
        messages=[
            {"role": "user", "content": "question initiale"},
            {"role": "assistant", "content": "réponse finale"},
        ],
    )
    text = session_squash._fallback_summary(sess, SID, "modèle auxiliaire indisponible")
    assert len(text) >= session_squash.MIN_SUMMARY_CHARS
    for section in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7.", "## 8.", "## 9."):
        assert section in text
    assert SID in text
    assert "modèle auxiliaire indisponible" in text


def test_mobile_context_panel_contains_squash_action():
    """Mobile must expose squash below the Context card, not only in the
    desktop composer footer where narrow-layout CSS hides it."""
    repo = Path(__file__).resolve().parent.parent
    html = (repo / "static" / "index.html").read_text(encoding="utf-8")
    css = (repo / "static" / "style.css").read_text(encoding="utf-8")
    js = (repo / "static" / "panels.js").read_text(encoding="utf-8")

    context_pos = html.index('id="composerMobileContextAction"')
    squash_pos = html.index('id="composerMobileSquashBtn"')
    panel_end = html.index("</div>", squash_pos)
    assert context_pos < squash_pos < panel_end
    assert 'onclick="closeMobileComposerConfig();squashConversation()"' in html
    assert "composer-mobile-config-panel .composer-mobile-squash-action{flex:1 0 100%;width:100%" in css
    assert "$('composerMobileSquashBtn')" in js


# ── #6704 P1 focused regressions ─────────────────────────────────────────

def test_cancelled_writeback_cannot_survive_squash(tmp_path):
    """Focused regression for the failing production ordering (#6704 P1):

    turn admitted (writeback ownership registered) → operator stops it →
    cancel_stream() clears the live/busy indicators eagerly while the worker
    is still unwinding (ownership NOT yet released) → operator starts squash.

    Admission must fail closed on the surviving ownership record: while it
    exists the old worker may still save its pre-squash snapshot, silently
    restoring the archived transcript after the squash reported success.
    Once the worker's own ``finally`` releases ownership, squash must
    proceed — and during the mutation the ownership slot must hold the
    squash tombstone, so a late ownership-gated finalizer from the old
    stream compares against it and fails closed instead of matching an
    empty slot.
    """
    from api import config as api_config

    sess = _make_session(tmp_path)  # cancel already cleared the busy indicators
    original = sess.path.read_bytes()

    # The cancelled worker still owns the writeback (its finally has not run).
    api_config.register_session_writeback_owner(SID, "stream-old")
    try:
        with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess):
            with pytest.raises(session_squash.SquashError) as excinfo:
                session_squash.start_squash_job(SID, confirm_session_id=SID, summary=None)
        assert excinfo.value.status == 409
        assert "writeback" in str(excinfo.value)
        # Fail-closed means NO mutation and NO archive: the transcript on disk
        # is byte-identical and nothing was archived.
        assert sess.path.read_bytes() == original
        assert not (sess.path.parent.parent / "session-squash-archives").exists()
    finally:
        # The worker's own finally releases ownership — only now may squash run.
        api_config.clear_session_writeback_owner_if_owned(SID, "stream-old")

    owners_seen = []
    real_save = sess.save

    def _spy_save(**kwargs):
        owners_seen.append(api_config.session_writeback_owner(SID))
        real_save(**kwargs)

    sess.save = _spy_save
    snap = _run_job(sess)
    assert snap["status"] == "done", snap.get("error")
    assert snap["result"]["already_squashed"] is False
    # During the squash save the ownership slot held the squash tombstone: an
    # ownership-gated finalizer from the cancelled stream ("stream-old") would
    # see owner != its stream_id and skip its writeback (fail closed).
    assert owners_seen, "squash never saved"
    assert all(owner and owner.startswith("squash-") for owner in owners_seen), owners_seen
    # The tombstone is released afterwards (conditionally: a successor turn's
    # own entry would never be clobbered).
    assert api_config.session_writeback_owner(SID) is None
    persisted = json.loads(sess.path.read_text(encoding="utf-8"))
    assert len(persisted["messages"]) == 1
    assert persisted["messages"][0]["_squash_summary"] is True


def test_squash_job_recheck_refuses_ownership_registered_after_admission(tmp_path):
    """The admission pre-check runs without the per-session agent lock, so a
    turn admitted-and-cancelled in that window leaves a surviving ownership
    record the pre-check never saw. The under-lock re-check inside the job
    worker must fail closed on it — busy indicators alone are not evidence
    of idleness."""
    from api import config as api_config

    sess = _make_session(tmp_path)
    original = sess.path.read_bytes()
    api_config.register_session_writeback_owner(SID, "stream-old")
    job = {
        "job_id": "recheck-test-job",
        "session_id": SID,
        "title": "session de test",
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    try:
        with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess), \
             patch.object(api.session_ops, "_live_active_stream_id", lambda _s: None), \
             patch.object(api.routes, "_get_session_agent_lock", _dummy_lock), \
             patch.object(api.routes, "_publish_session_list_changed", lambda *a, **k: None), \
             patch("api.config._evict_session_agent", lambda _sid: None):
            session_squash._run_squash_job(job, "synthèse fournie " * 40)
        assert job["status"] == "error", job
        assert "writeback" in (job["error"] or "")
        assert sess.path.read_bytes() == original
        assert not (sess.path.parent.parent / "session-squash-archives").exists()
        # The refusal must not release the old worker's ownership entry.
        assert api_config.session_writeback_owner(SID) == "stream-old"
    finally:
        api_config.clear_session_writeback_owner_if_owned(SID, "stream-old")


def test_squash_completion_reload_is_conditional_on_current_session():
    """The squash flow must use captured requested-navigation authority."""
    repo = Path(__file__).resolve().parent.parent
    js = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    fn_start = js.index("async function squashConversation")
    fn_end = js.index("function _pollSquashJob")
    body = js[fn_start:fn_end]
    assert "const navigationAuthority = _captureSessionNavigationAuthority(sid);" in body
    assert "if(_sessionNavigationAuthorityIsCurrent(navigationAuthority))" in body
    assert "await loadSession(sid, {force: true});" in body


def test_squash_running_indicator_is_owner_scoped_wiring():
    """Focused regression (#6704 P1 follow-up): 'squash-running' renders on the
    SHARED desktop/mobile controls, so it must be keyed by the owning session
    (upload-bar pattern) and re-synced on every session switch — not toggled
    unconditionally on whatever conversation happens to be displayed."""
    repo = Path(__file__).resolve().parent.parent
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    sessions = (repo / "static" / "sessions.js").read_text(encoding="utf-8")

    # The per-owner state + sync/set helpers exist.
    assert "const _squashRunningSessions = new Set()" in panels
    assert "function _squashSyncRunningIndicatorForSession(" in panels
    assert "function _squashSetRunning(" in panels

    # squashConversation must go through the owner-scoped setter, never flip
    # the shared class directly on the buttons.
    fn_start = panels.index("async function squashConversation")
    fn_end = panels.index("function _pollSquashJob")
    body = panels[fn_start:fn_end]
    assert "_squashSetRunning(sid, true)" in body
    assert "_squashSetRunning(sid, false)" in body
    assert "classList.add('squash-running')" not in body
    assert "classList.remove('squash-running')" not in body

    # loadSession re-syncs the shared controls against the displayed session
    # on switch, alongside the existing upload-bar owner resync.
    ls_start = sessions.index("async function loadSession")
    ls_body = sessions[ls_start : sessions.index("\nasync function", ls_start + 10)]
    assert "_squashSyncRunningIndicatorForSession(sid)" in ls_body


def test_squash_running_indicator_does_not_leak_across_sessions_runtime():
    """Behavioral regression (#6704 P1 follow-up), running the REAL helper
    block from panels.js in node's ``vm``:

    start a squash on session A → switch to session B mid-job → the shared
    desktop+mobile controls must drop 'squash-running'; switch back to A →
    the indicator re-asserts; the job settles while B is displayed → B's
    controls stay clean (no flash, no stale removal on the wrong session).
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    repo = Path(__file__).resolve().parent.parent
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    start = panels.index("const _squashRunningSessions = new Set()")
    end = panels.index("async function squashConversation")
    helpers = panels[start:end]
    harness = textwrap.dedent(
        """
        'use strict';
        const vm = require('vm');
        function makeBtn(){
          const classes = new Set();
          return {classes, classList: {
            toggle(name, force){ if(force) classes.add(name); else classes.delete(name); },
            add(name){ classes.add(name); },
            remove(name){ classes.delete(name); },
            contains(name){ return classes.has(name); },
          }};
        }
        const desktop = makeBtn();
        const mobile = makeBtn();
        const ctx = {
          $: (id) => (id === 'btnSquash' ? desktop : (id === 'composerMobileSquashBtn' ? mobile : null)),
          S: {session: {session_id: 'sess-A'}},
        };
        vm.createContext(ctx);
        vm.runInContext(HELPERS_SRC, ctx);
        const running = () => [desktop, mobile].map(b => b.classList.contains('squash-running'));
        const out = {};
        // Viewing A, squash starts on A -> both shared controls pulse.
        vm.runInContext("_squashSetRunning('sess-A', true)", ctx);
        out.owner_shows = running();
        // User switches to B mid-job (loadSession resyncs for the new sid).
        ctx.S.session = {session_id: 'sess-B'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-B')", ctx);
        out.other_session_clean = running();
        // Back to the owner while the job is still running -> re-asserts.
        ctx.S.session = {session_id: 'sess-A'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-A')", ctx);
        out.owner_reasserts = running();
        // Switch to B again; the job settles while B is displayed -> B stays clean.
        ctx.S.session = {session_id: 'sess-B'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-B')", ctx);
        vm.runInContext("_squashSetRunning('sess-A', false)", ctx);
        out.settle_on_other_session_clean = running();
        // Back on A after settle -> nothing lingers.
        ctx.S.session = {session_id: 'sess-A'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-A')", ctx);
        out.owner_clean_after_settle = running();
        console.log(JSON.stringify(out));
        """
    ).replace("HELPERS_SRC", json.dumps(helpers))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["owner_shows"] == [True, True]
    assert out["other_session_clean"] == [False, False], (
        "P1 leak: 'squash-running' must clear on the shared controls when a "
        "different conversation is displayed while the job runs"
    )
    assert out["owner_reasserts"] == [True, True]
    assert out["settle_on_other_session_clean"] == [False, False]
    assert out["owner_clean_after_settle"] == [False, False]


def test_squash_completion_obeys_requested_navigation_runtime():
    """Run the real squash flow against the real navigation-authority helpers.

    Metadata requests are controlled promises so the production race is exact:
    session B has been requested, but ``S.session`` still points at A when A's
    squash status settles.  The newer requested-navigation generation must own
    the view even when B is pending or fails.  An explicit B -> A request also
    owns A without a second completion reload.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    repo = Path(__file__).resolve().parent.parent
    sessions = (repo / "static" / "sessions.js").read_text(encoding="utf-8")
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    nav_start = sessions.index("let _loadingSessionId = null")
    nav_end = sessions.index("// #3306:", nav_start)
    navigation_authority = sessions[nav_start:nav_end]
    load_start = sessions.index("async function loadSession(")
    load_end = sessions.index("\n// ── Handoff hint logic", load_start)
    load_body = sessions[load_start:load_end]
    assert "const _loadGeneration = _beginSessionNavigationRequest(sid);" in load_body
    assert "_sessionNavigationRequestIsCurrent(sid,_loadGeneration)" in load_body
    assert "_squashSyncRunningIndicatorForSession(sid)" in load_body
    assert "if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid))" in load_body
    squash_start = panels.index("const _squashRunningSessions")
    squash_end = panels.index("// ── Skills panel", squash_start)
    squash_flow = panels[squash_start:squash_end]

    harness = textwrap.dedent(
        r"""
        'use strict';
        const vm = require('vm');

        function deferred(){
          let resolve, reject;
          const promise = new Promise((res, rej)=>{ resolve=res; reject=rej; });
          return {promise, resolve, reject};
        }
        const settle = async()=>{
          await new Promise(resolve=>setImmediate(resolve));
          await new Promise(resolve=>setImmediate(resolve));
        };
        function makeButton(){
          const classes = new Set();
          return {classList:{
            toggle(name, force){ if(force) classes.add(name); else classes.delete(name); },
            contains(name){ return classes.has(name); },
          }};
        }
        function makeRuntime(){
          const status = deferred();
          const metadata = [];
          const loads = [];
          const loadErrors = [];
          const toasts = [];
          const desktop = makeButton();
          const mobile = makeButton();
          const ctx = {
            S:{
              session:{session_id:'sess-A'},
              messages:[{role:'assistant',content:'A'}],
              toolCalls:[], pendingFiles:[],
            },
            $:(id)=>id==='btnSquash'?desktop:(id==='composerMobileSquashBtn'?mobile:null),
            t:(key)=>key,
            showConfirmDialog:async()=>true,
            showToast:(...args)=>toasts.push(args),
            api:async(url, opts)=>{
              if(url==='/api/session/squash') return {job:{job_id:'job-A'}};
              if(url.startsWith('/api/session/squash/status')) return status.promise;
              throw new Error('unexpected api '+url+' '+JSON.stringify(opts||{}));
            },
            _requestMetadata:(sid)=>{
              const request=deferred();
              metadata.push({sid, request});
              return request.promise;
            },
            _loads:loads,
            _loadErrors:loadErrors,
            setTimeout,
            clearTimeout,
            console,
          };
          vm.createContext(ctx);
          vm.runInContext(NAVIGATION_AUTHORITY_SRC, ctx);
          vm.runInContext(SQUASH_FLOW_SRC, ctx);
          // This small runtime adapter deliberately uses the SAME request/current
          // helpers as production loadSession.  Controlled metadata promises keep
          // S.session on A until the requested B response is explicitly released.
          vm.runInContext(`
            async function loadSession(sid, opts={}){
              const currentSid=S.session&&S.session.session_id;
              const forceReload=!!opts.force;
              if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid)) return;
              const generation=_beginSessionNavigationRequest(sid);
              _loads.push({sid, force:!!opts.force, generation});
              if(typeof _squashSyncRunningIndicatorForSession==='function'){
                _squashSyncRunningIndicatorForSession(sid);
              }
              try{
                const data=await _requestMetadata(sid);
                if(!_sessionNavigationRequestIsCurrent(sid,generation)) return;
                S.session=data.session;
                _loadingSessionId=null;
              }catch(error){
                _loadErrors.push({sid,message:String(error&&error.message||error)});
                if(_sessionNavigationRequestIsCurrent(sid,generation)) _loadingSessionId=null;
              }
            }
          `, ctx);
          return {
            ctx, status, metadata, loads, loadErrors, toasts, desktop, mobile,
            buttons:()=>[desktop,mobile].map(btn=>btn.classList.contains('squash-running')),
          };
        }
        const run=(rt, source)=>vm.runInContext(source, rt.ctx);
        const done=(sessionId='sess-A')=>({job:{
          job_id:'job-A', session_id:sessionId, status:'done',
          result:{already_squashed:false,before:{message_count:4},after:{message_count:1}},
        }});
        const requestFor=(rt, sid, index=0)=>rt.metadata.filter(item=>item.sid===sid)[index];
        function startSquash(rt){
          return {promise:run(rt,'squashConversation()'), ready:settle()};
        }

        (async()=>{
          const out={};

          // B metadata is pending when A completes: B remains authoritative.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            out.pending_b_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            const unexpectedA=requestFor(rt,'sess-A');
            if(unexpectedA) unexpectedA.request.resolve({session:{session_id:'sess-A'}});
            requestFor(rt,'sess-B').request.resolve({session:{session_id:'sess-B'}});
            await Promise.all([squash,navB]);
            out.pending_b={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // With no newer navigation, completion owns exactly one forced A refresh.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            out.owner_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A').request.resolve({session:{session_id:'sess-A'}});
            await squash;
            out.no_navigation={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // B -> explicit A: the explicit A request wins; completion must not
          // start a duplicate A load while that metadata request is pending.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            const navA=run(rt,"loadSession('sess-A')");
            await settle();
            out.back_a_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A').request.resolve({session:{session_id:'sess-A'}});
            requestFor(rt,'sess-B').request.resolve({session:{session_id:'sess-B'}});
            await Promise.all([squash,navA,navB]);
            out.back_a={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // A failed B request remains a newer user choice.  Its error must not
          // be erased by a late squash-completion navigation back to A.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            requestFor(rt,'sess-B').request.reject(new Error('B metadata failed'));
            await navB;
            rt.status.resolve(done());
            await settle();
            const unexpectedA=requestFor(rt,'sess-A');
            if(unexpectedA) unexpectedA.request.resolve({session:{session_id:'sess-A'}});
            await squash;
            out.failed_b={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              errors:rt.loadErrors.map(x=>x.sid),
            };
          }

          // Poll results are owner-scoped too: a B job can never complete A's
          // progress owner or trigger A's reload.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            rt.status.resolve(done('sess-B'));
            await squash;
            out.wrong_owner={
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
              failed:rt.toasts.some(args=>String(args[0]).includes('squash_failed')),
            };
          }

          console.log(JSON.stringify(out));
        })().catch(error=>{
          console.error(error&&error.stack||error);
          process.exitCode=1;
        });
        """
    ).replace("NAVIGATION_AUTHORITY_SRC", json.dumps(navigation_authority)).replace(
        "SQUASH_FLOW_SRC", json.dumps(squash_flow)
    )
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["pending_b_buttons"] == [False, False]
    assert out["pending_b"] == {
        "active": "sess-B", "loads": ["sess-B:false"], "buttons": [False, False],
    }
    assert out["owner_buttons"] == [True, True]
    assert out["no_navigation"] == {
        "active": "sess-A", "loads": ["sess-A:true"], "buttons": [False, False],
    }
    assert out["back_a_buttons"] == [True, True]
    assert out["back_a"] == {
        "active": "sess-A",
        "loads": ["sess-B:false", "sess-A:false"],
        "buttons": [False, False],
    }
    assert out["failed_b"] == {
        "active": "sess-A", "loads": ["sess-B:false"], "errors": ["sess-B"],
    }
    assert out["wrong_owner"] == {
        "loads": [], "buttons": [False, False], "failed": True,
    }
