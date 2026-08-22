import json
import sqlite3
import subprocess
from collections import OrderedDict
from pathlib import Path

import pytest

from api.agent_sessions import read_session_lineage_ids

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_lineage_ids_enumerates_complete_continuation_tree_only(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, started_at REAL, ended_at REAL, source TEXT, session_source TEXT)")
    rows = [
        ("root", None, "compression", 1, 2, "cli", None),
        ("tip-a", "root", None, 3, None, "cli", None),
        ("mid-b", "root", "compression", 3, 4, "cli", None),
        ("tip-b", "mid-b", None, 5, None, "cli", None),
        ("fork", "root", None, 3, None, "cli", "fork"),
        ("child", "root", None, 1, None, "cli", None),
        ("cross-source", "root", None, 3, None, "tui", None),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    assert set(read_session_lineage_ids(db, "tip-b")) == {"root", "tip-a", "mid-b", "tip-b"}


def test_lineage_ids_excludes_cross_profile_continuations(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, started_at REAL, "
        "ended_at REAL, source TEXT, session_source TEXT, profile TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        [
            ("root", None, "compression", 1, 2, "cli", None, "default"),
            ("own-tip", "root", None, 3, None, "cli", None, "default"),
            ("foreign-tip", "root", None, 3, None, "cli", None, "research"),
        ],
    )
    conn.commit()
    conn.close()

    assert set(read_session_lineage_ids(db, "own-tip", "default")) == {"root", "own-tip"}
    assert read_session_lineage_ids(db, "foreign-tip", "default") == []


def test_lineage_ids_uses_renamed_root_profile_aliases(tmp_path, monkeypatch):
    import api.profiles as profiles

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, started_at REAL, "
        "ended_at REAL, source TEXT, session_source TEXT, profile TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        [
            ("root", None, "compression", 1, 2, "cli", None, "default"),
            ("legacy-tip", "root", None, 3, None, "cli", None, None),
            ("named-root-tip", "root", None, 3, None, "cli", None, "kinni"),
            ("foreign-tip", "root", None, 3, None, "cli", None, "research"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "kinni", "is_default": True, "path": str(tmp_path)}],
    )
    profiles._invalidate_root_profile_cache()
    try:
        assert set(read_session_lineage_ids(db, "legacy-tip", "kinni")) == {
            "root",
            "legacy-tip",
            "named-root-tip",
        }
        assert read_session_lineage_ids(db, "foreign-tip", "kinni") == []
        assert read_session_lineage_ids(db, "legacy-tip", "research") == []
    finally:
        profiles._invalidate_root_profile_cache()


def test_lineage_ids_without_profile_column_stay_reachable_for_named_profiles(tmp_path):
    """A profile-local state.db without sessions.profile must not 404 lineages.

    Every row in such a database belongs to the requesting profile, so the
    row-level profile filter cannot apply; the route's per-materialized-session
    visibility prevalidation remains the profile authority.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, "
        "started_at REAL, ended_at REAL, source TEXT, session_source TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        [
            ("root", None, "compression", 1, 2, "cli", None),
            ("tip", "root", None, 3, None, "cli", None),
        ],
    )
    conn.commit()
    conn.close()

    assert set(read_session_lineage_ids(db, "tip", "research")) == {"root", "tip"}
    assert set(read_session_lineage_ids(db, "tip", "default")) == {"root", "tip"}


@pytest.fixture
def lineage_session_store(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(
        models,
        "_PERSISTED_SESSION_IDS_CACHE",
        (None, None, frozenset()),
    )
    return session_dir


def _lineage_sessions(session_dir, *, archived=False):
    from api.models import Session

    sessions = []
    for sid in ("lineage-root", "lineage-tip"):
        session = Session(
            session_id=sid,
            title=sid,
            workspace="",
            model="test-model",
            profile="default",
            messages=[{"role": "user", "content": sid}],
        )
        session.archived = archived
        session.save(touch_updated_at=False)
        sessions.append(session)
    assert (session_dir / "_index.json").exists()
    return sessions


def _durable_images(session_dir):
    return {
        path.name: path.read_bytes()
        for path in sorted(session_dir.glob("*.json"))
    }


def _assert_cold_archive_parity(session_dir, expected):
    from api.models import Session

    index = json.loads((session_dir / "_index.json").read_text(encoding="utf-8"))
    by_id = {entry["session_id"]: entry for entry in index}
    for sid in ("lineage-root", "lineage-tip"):
        assert Session.load(sid).archived is expected
        assert by_id[sid]["archived"] is expected


@pytest.mark.parametrize("failed_target", ["lineage-tip.json", "_index.json"])
def test_lineage_batch_rolls_back_every_sidecar_and_index_after_publication_failure(
    lineage_session_store,
    monkeypatch,
    failed_target,
):
    """A current/later sidecar or final index failure restores byte-exact preimages."""
    import api.models as models
    import api.session_batch_transaction as transaction

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    with models.LOCK:
        for session in sessions:
            models.SESSIONS[session.session_id] = session
        cached_before = list(models.SESSIONS.items())
    before = _durable_images(lineage_session_store)
    original_replace = transaction._replace_bytes
    failed = False
    published_before_failure = []

    def fail_once(path, payload):
        nonlocal failed
        if path.name == failed_target and not failed:
            failed = True
            raise OSError("injected publication failure")
        result = original_replace(path, payload)
        if not failed and path.name in {"lineage-root.json", "lineage-tip.json"}:
            published_before_failure.append(path.name)
        return result

    monkeypatch.setattr(transaction, "_replace_bytes", fail_once)
    with pytest.raises(transaction.SessionBatchTransactionError) as caught:
        transaction.commit_session_archive_batch(sessions, True)

    assert failed is True
    if failed_target == "_index.json":
        assert published_before_failure == ["lineage-root.json", "lineage-tip.json"]
    else:
        assert published_before_failure == ["lineage-root.json"]
    assert caught.value.phase == "publication"
    assert caught.value.recovery_required is False
    assert _durable_images(lineage_session_store) == before
    assert [session.archived for session in sessions] == [False, False]
    with models.LOCK:
        assert list(models.SESSIONS.items()) == cached_before
    assert not (lineage_session_store / transaction._JOURNAL_NAME).exists()
    _assert_cold_archive_parity(lineage_session_store, False)


def test_lineage_batch_compensation_failure_is_durably_recovered(
    lineage_session_store,
    monkeypatch,
):
    """Failed inline compensation leaves a journal that startup recovery completes."""
    import api.session_batch_transaction as transaction

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    before = _durable_images(lineage_session_store)
    original_replace = transaction._replace_bytes
    state = {"index_failed": False, "allow_recovery": False}

    def fail_publish_and_compensation(path, payload):
        if path.name == "_index.json" and not state["index_failed"]:
            state["index_failed"] = True
            raise OSError("injected index publication failure")
        if state["index_failed"] and not state["allow_recovery"] and path.name == "lineage-root.json":
            raise OSError("injected compensation failure")
        return original_replace(path, payload)

    monkeypatch.setattr(transaction, "_replace_bytes", fail_publish_and_compensation)
    with pytest.raises(transaction.SessionBatchTransactionError) as caught:
        transaction.commit_session_archive_batch(sessions, True)

    assert caught.value.recovery_required is True
    assert caught.value.recovery_errors == ["lineage-root.json:OSError"]
    assert (lineage_session_store / transaction._JOURNAL_NAME).exists()
    assert [session.archived for session in sessions] == [False, False]

    state["allow_recovery"] = True
    recovered = transaction.recover_pending_session_batch(lineage_session_store)
    assert recovered["found"] is True
    assert recovered["recovered"] is True
    assert recovered["decision"] == "rollback"
    assert _durable_images(lineage_session_store) == before
    _assert_cold_archive_parity(lineage_session_store, False)


def test_committed_lineage_batch_journal_rolls_forward_on_recovery(
    lineage_session_store,
    monkeypatch,
):
    """A crash-stale commit journal repairs any torn file to the committed image."""
    import api.session_batch_transaction as transaction

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    before = _durable_images(lineage_session_store)
    original_remove = transaction._remove_path

    def leave_committed_journal(path):
        if path.name == transaction._JOURNAL_NAME:
            raise OSError("injected crash before journal cleanup")
        return original_remove(path)

    monkeypatch.setattr(transaction, "_remove_path", leave_committed_journal)
    transaction.commit_session_archive_batch(sessions, True)
    journal = lineage_session_store / transaction._JOURNAL_NAME
    assert journal.exists()

    # Simulate one committed sidecar being lost/torn before restart. The
    # committed journal must roll the complete new image forward, not rollback.
    transaction._replace_bytes(
        lineage_session_store / "lineage-root.json",
        before["lineage-root.json"],
    )
    monkeypatch.setattr(transaction, "_remove_path", original_remove)
    recovered = transaction.recover_pending_session_batch(lineage_session_store)

    assert recovered["found"] is True
    assert recovered["recovered"] is True
    assert recovered["decision"] == "commit"
    assert not journal.exists()
    _assert_cold_archive_parity(lineage_session_store, True)


def test_startup_recovery_replays_batch_journal_through_production_wiring(
    lineage_session_store,
    monkeypatch,
):
    """server.py's recovery entrypoint replays a stale rollback journal itself."""
    import api.session_batch_transaction as transaction
    from api.session_recovery import run_startup_session_recovery

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    before = _durable_images(lineage_session_store)
    original_replace = transaction._replace_bytes
    state = {"index_failed": False, "allow_recovery": False}

    def fail_publish_and_compensation(path, payload):
        if path.name == "_index.json" and not state["index_failed"]:
            state["index_failed"] = True
            raise OSError("injected index publication failure")
        if state["index_failed"] and not state["allow_recovery"] and path.name == "lineage-root.json":
            raise OSError("injected compensation failure")
        return original_replace(path, payload)

    monkeypatch.setattr(transaction, "_replace_bytes", fail_publish_and_compensation)
    with pytest.raises(transaction.SessionBatchTransactionError):
        transaction.commit_session_archive_batch(sessions, True)
    assert (lineage_session_store / transaction._JOURNAL_NAME).exists()

    state["allow_recovery"] = True
    run_startup_session_recovery(lineage_session_store)

    assert not (lineage_session_store / transaction._JOURNAL_NAME).exists()
    assert _durable_images(lineage_session_store) == before
    _assert_cold_archive_parity(lineage_session_store, False)


@pytest.mark.parametrize("target_archived", [True, False], ids=["archive", "restore"])
@pytest.mark.parametrize("leave_committed_journal", [True, False], ids=["journal-replay", "ordinary-restart"])
def test_startup_backup_recovery_preserves_transactional_lineage_archive_state(
    lineage_session_store,
    monkeypatch,
    target_archived,
    leave_committed_journal,
):
    """Legacy transcript recovery must compose with lineage archive publication.

    A stale rescue backup owns the longer transcript, not newer archive or other
    session metadata.  Cover both startup immediately after a committed journal
    was left behind and an ordinary later restart after journal cleanup.
    """
    import api.session_batch_transaction as transaction
    from api.session_recovery import run_startup_session_recovery

    sessions = _lineage_sessions(
        lineage_session_store,
        archived=not target_archived,
    )
    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup_payload = json.loads(root_path.read_text(encoding="utf-8"))
    backup_payload.update(
        {
            "archived": not target_archived,
            "title": "stale backup title",
            "messages": [
                {"role": "user", "content": "rescued prompt"},
                {"role": "assistant", "content": "rescued answer"},
            ],
            "context_messages": [
                {"role": "user", "content": "rescued prompt"},
                {"role": "assistant", "content": "rescued answer"},
            ],
        }
    )
    backup_bytes = json.dumps(backup_payload, ensure_ascii=False, indent=2).encode("utf-8")
    backup_path.write_bytes(backup_bytes)

    original_remove = transaction._remove_path
    if leave_committed_journal:
        def leave_journal(path):
            if path.name == transaction._JOURNAL_NAME:
                raise OSError("injected crash before journal cleanup")
            return original_remove(path)

        monkeypatch.setattr(transaction, "_remove_path", leave_journal)

    transaction.commit_session_archive_batch(sessions, target_archived)
    journal_path = lineage_session_store / transaction._JOURNAL_NAME
    assert journal_path.exists() is leave_committed_journal
    monkeypatch.setattr(transaction, "_remove_path", original_remove)

    run_startup_session_recovery(lineage_session_store)

    assert not journal_path.exists()
    assert backup_path.read_bytes() == backup_bytes
    root = json.loads(root_path.read_text(encoding="utf-8"))
    tip = json.loads(
        (lineage_session_store / "lineage-tip.json").read_text(encoding="utf-8")
    )
    assert root["archived"] is target_archived
    assert tip["archived"] is target_archived
    assert root["title"] == "lineage-root"
    assert [message["content"] for message in root["messages"]] == [
        "rescued prompt",
        "rescued answer",
    ]
    assert root["context_messages"] == backup_payload["context_messages"]
    _assert_cold_archive_parity(lineage_session_store, target_archived)

    # The durable rescue snapshot remains available, and a normal restart after
    # journal cleanup is an idempotent no-op for both transcript and metadata.
    images_after_recovery = _durable_images(lineage_session_store)
    run_startup_session_recovery(lineage_session_store)
    assert _durable_images(lineage_session_store) == images_after_recovery
    assert backup_path.read_bytes() == backup_bytes
    _assert_cold_archive_parity(lineage_session_store, target_archived)


def test_startup_recovery_fails_closed_on_unrecoverable_batch_journal(
    lineage_session_store,
    monkeypatch,
):
    """An unreplayable batch journal aborts startup before best-effort repair."""
    import api.session_batch_transaction as transaction
    import api.session_recovery as session_recovery

    _lineage_sessions(lineage_session_store, archived=False)
    (lineage_session_store / transaction._JOURNAL_NAME).write_text(
        "{not json", encoding="utf-8"
    )
    legacy_calls = []
    monkeypatch.setattr(
        session_recovery,
        "recover_all_sessions_on_startup",
        lambda *args, **kwargs: legacy_calls.append((args, kwargs)) or {},
    )

    with pytest.raises(RuntimeError, match="session batch recovery remains incomplete"):
        session_recovery.run_startup_session_recovery(lineage_session_store)

    assert legacy_calls == []


def test_server_startup_uses_fail_closed_recovery_entrypoint():
    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "from api.session_recovery import run_startup_session_recovery" in server_src
    assert "run_startup_session_recovery(SESSION_DIR)" in server_src


@pytest.mark.parametrize(("initial", "target"), [(False, True), (True, False)])
def test_lineage_batch_archive_restore_symmetry_has_exact_sidecar_index_parity(
    lineage_session_store,
    initial,
    target,
):
    from api.session_batch_transaction import commit_session_archive_batch

    sessions = _lineage_sessions(lineage_session_store, archived=initial)
    transaction_id = commit_session_archive_batch(sessions, target)

    assert len(transaction_id) == 32
    assert [session.archived for session in sessions] == [target, target]
    _assert_cold_archive_parity(lineage_session_store, target)


def test_lineage_materialization_can_stage_without_publishing(
    lineage_session_store,
    monkeypatch,
):
    """Lineage prevalidation of a missing CLI sidecar performs no durable write."""
    import api.routes as routes

    def missing_session(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session", missing_session)
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "id": _sid,
            "title": "Staged CLI session",
            "model": "test-model",
            "profile": "default",
            "source": "cli",
        },
    )
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda _sid, **_kwargs: [{"role": "user", "content": "hello"}],
    )

    session = routes._get_or_materialize_session("lineage-staged", persist=False)

    assert session.session_id == "lineage-staged"
    assert session.messages == [{"role": "user", "content": "hello"}]
    assert list(lineage_session_store.iterdir()) == []


def test_lineage_route_reports_durable_recovery_disposition(monkeypatch, tmp_path):
    """A residual mixed publication is never hidden behind a generic 500."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    import api.routes as routes
    from api.session_batch_transaction import SessionBatchTransactionError

    captured = {}
    session = SimpleNamespace(session_id="lineage-root", profile="default", archived=False)
    failure = SessionBatchTransactionError(
        "Lineage archive transaction publication failed (OSError)",
        transaction_id="abc123",
        phase="publication",
        recovery_required=True,
        recovery_errors=["lineage-root.json:OSError"],
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": "lineage-root", "archived": True, "lineage": True},
    )
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "read_session_lineage_ids", lambda *_args: ["lineage-root"])
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: nullcontext())
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "commit_session_archive_batch", lambda *_args: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(payload=payload, status=status) or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert captured == {
        "status": 503,
        "payload": {
            "error": "Lineage archive transaction publication failed (OSError)",
            "transaction_id": "abc123",
            "phase": "publication",
            "recovery_required": True,
            "recovery_errors": ["lineage-root.json:OSError"],
        },
    }


def test_lineage_archive_rechecks_scope_while_all_target_locks_are_held():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = routes.index('        if body.get("lineage"):\n')
    end = routes.index("        if _session_is_subagent_view_only(sid):\n", start)
    archive = routes[start:end]

    assert "with ExitStack() as locks:" in archive
    assert "for lineage_sid in sorted(lineage_ids):" in archive
    assert archive.index("locks.enter_context(_get_session_agent_lock(lineage_sid))") < archive.index(
        "current_ids = read_session_lineage_ids(state_db_path, sid, request_profile)"
    )
    assert "if set(current_ids) != set(lineage_ids):" in archive
    assert "read_session_lineage_ids(state_db_path, sid, request_profile)" in archive
    assert "_session_visible_to_active_profile(getattr(session, \"profile\", None), handler)" in archive
    assert 'return bad(handler, "Session lineage changed during archive; retry", 409)' in archive


def test_archive_always_uses_one_backend_lineage_operation():
    assert "const payload={session_id:session.session_id,archived,lineage:true}" in SESSIONS_JS
    assert "Promise.all(targets.map" not in SESSIONS_JS
    script = f"""
const src={SESSIONS_JS!r};
const start=src.indexOf('async function _archiveSession('); let brace=src.indexOf('{{',start), depth=0, end=-1;
for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
eval(src.slice(start,end));
const calls=[];
Object.assign(globalThis,{{
  _isReadOnlySession:()=>false,_captureSessionReflowPositions:()=>new Map(),_sessionSegmentCount:()=>3,
  api:async(p,o)=>{{calls.push(JSON.parse(o.body));return {{session_ids:['root','tip']}};}},
  _allSessions:[],S:{{session:null}},localStorage:{{getItem:()=>null,removeItem:()=>{{}}}},showToast:()=>{{}},
  _sessionArchiveToast:()=>'',t:x=>x,_showArchived:false,_sessionPrefersReducedMotion:()=>true,
  _sessionSwipeReturnOffsets:new Map(),renderSessionListFromCache:()=>{{}},renderSessionList:async()=>{{}}
}});
_archiveSession({{session_id:'tip',archived:false}},true).then(()=>console.log(JSON.stringify(calls)));
"""
    proc = subprocess.run(["node"], input=script, text=True, capture_output=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"session_id": "tip", "archived": True, "lineage": True}]


def test_archive_does_not_trust_bounded_sidebar_metadata():
    """A row beyond the top-300 enrichment cap still delegates scope to the server."""
    script = f"""
const src={SESSIONS_JS!r};
const start=src.indexOf('async function _archiveSession('); let brace=src.indexOf('{{',start), depth=0, end=-1;
for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
eval(src.slice(start,end));
const calls=[];
Object.assign(globalThis,{{
  _isReadOnlySession:()=>false,_captureSessionReflowPositions:()=>new Map(),
  _sessionSegmentCount:()=>0,
  api:async(p,o)=>{{calls.push(JSON.parse(o.body));return {{session_ids:['hidden-root','row-301']}};}},
  _allSessions:Array.from({{length:301}},(_,i)=>({{session_id:`row-${{i+1}}`,archived:false}})),
  S:{{session:null}},localStorage:{{getItem:()=>null,removeItem:()=>{{}}}},showToast:()=>{{}},
  _sessionArchiveToast:()=>'',t:x=>x,_showArchived:false,_sessionPrefersReducedMotion:()=>true,
  _sessionSwipeReturnOffsets:new Map(),renderSessionListFromCache:()=>{{}},renderSessionList:async()=>{{}}
}});
_archiveSession(_allSessions[300],true).then(()=>console.log(JSON.stringify(calls)));
"""
    proc = subprocess.run(["node"], input=script, text=True, capture_output=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        {"session_id": "row-301", "archived": True, "lineage": True}
    ]
