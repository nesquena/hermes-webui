import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections import OrderedDict
from contextlib import ExitStack
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


def _duplicate_partial_messages(label):
    partial = {
        "role": "assistant",
        "content": f"partial-{label}",
        "_partial": True,
    }
    return [
        {"role": "user", "content": label},
        dict(partial),
        dict(partial),
    ]


def test_session_load_self_heal_cannot_revert_committed_lineage_archive(
    lineage_session_store,
    monkeypatch,
):
    """A pre-archive self-heal snapshot must not publish after the batch commit."""
    import api.models as models
    import api.session_batch_transaction as transaction

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    sessions[0].messages = _duplicate_partial_messages("lineage-root")
    sessions[0].save(touch_updated_at=False)

    read_complete = threading.Event()
    resume_self_heal = threading.Event()
    original_collapse = models._collapse_adjacent_duplicate_partials
    pause_lock = threading.Lock()
    should_pause = True

    def pause_after_real_collapse(messages):
        nonlocal should_pause
        collapsed, changed = original_collapse(messages)
        with pause_lock:
            pause_now = changed and should_pause
            if pause_now:
                should_pause = False
        if pause_now:
            read_complete.set()
            assert resume_self_heal.wait(5), "timed out waiting to resume Session.load self-heal"
        return collapsed, changed

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", pause_after_real_collapse)
    loaded = []
    errors = []

    def load_root():
        try:
            loaded.append(models.Session.load("lineage-root"))
        except BaseException as exc:  # surfaced in the parent assertion below
            errors.append(exc)

    reader = threading.Thread(target=load_root)
    reader.start()
    assert read_complete.wait(5), "Session.load did not reach duplicate-partial self-heal"

    with ExitStack() as stack:
        for session in sorted(sessions, key=lambda item: item.session_id):
            stack.enter_context(models._get_session_agent_lock(session.session_id))
        transaction.commit_session_archive_batch(sessions, True)

    resume_self_heal.set()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert errors == []
    assert loaded and loaded[0] is not None
    assert loaded[0].archived is True

    sidecars = {
        sid: json.loads((lineage_session_store / f"{sid}.json").read_text(encoding="utf-8"))
        for sid in ("lineage-root", "lineage-tip")
    }
    index = {
        row["session_id"]: row
        for row in json.loads((lineage_session_store / "_index.json").read_text(encoding="utf-8"))
    }
    for sid in ("lineage-root", "lineage-tip"):
        assert sidecars[sid]["archived"] is True
        assert index[sid]["archived"] is True
    assert len(sidecars["lineage-root"]["messages"]) == 2


def test_session_load_self_heal_obeys_cross_process_store_lock(
    lineage_session_store,
    monkeypatch,
):
    """The real write-capable load path participates in the advisory store lock."""
    import api.models as models

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    sessions[0].messages = _duplicate_partial_messages("lineage-root")
    sessions[0].save(touch_updated_at=False)

    child_script = """
import sys
from pathlib import Path
from api.session_batch_transaction import session_store_transaction_lock

with session_store_transaction_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""
    child_env = os.environ.copy()
    child_env["HERMES_HOME"] = str(lineage_session_store.parent / "child-hermes-home")
    child_env["HERMES_WEBUI_STATE_DIR"] = str(lineage_session_store.parent / "child-webui-state")
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(lineage_session_store)],
        cwd=ROOT,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"

    read_complete = threading.Event()
    load_complete = threading.Event()
    original_collapse = models._collapse_adjacent_duplicate_partials

    def observe_real_collapse(messages):
        collapsed, changed = original_collapse(messages)
        if changed:
            read_complete.set()
        return collapsed, changed

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", observe_real_collapse)
    errors = []

    def load_root():
        try:
            models.Session.load("lineage-root")
        except BaseException as exc:  # surfaced in the parent assertion below
            errors.append(exc)
        finally:
            load_complete.set()

    reader = threading.Thread(target=load_root)
    reader.start()
    assert read_complete.wait(5), "Session.load did not read the duplicate-partial sidecar"
    blocked_by_child = not load_complete.wait(0.5)

    assert child.stdin is not None
    child.stdin.write("\n")
    child.stdin.flush()
    child.stdin.close()
    reader.join(timeout=5)
    child_stderr = child.stderr.read() if child.stderr is not None else ""
    child_rc = child.wait(timeout=5)

    assert child_rc == 0, child_stderr
    assert blocked_by_child, "Session.load self-heal bypassed the cross-process store lock"
    assert not reader.is_alive()
    assert errors == []


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


def test_published_lineage_batch_journal_never_replays_over_later_writes(
    lineage_session_store,
    monkeypatch,
):
    """A cleanup-stale commit marker cannot roll back later session/index writes."""
    from api.models import Session
    import api.session_batch_transaction as transaction
    from api.session_recovery import run_startup_session_recovery

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    original_remove = transaction._remove_path

    def leave_committed_journal(path):
        if path.name == transaction._JOURNAL_NAME:
            raise OSError("injected journal cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(transaction, "_remove_path", leave_committed_journal)
    transaction.commit_session_archive_batch(sessions, True)
    journal = lineage_session_store / transaction._JOURNAL_NAME
    assert journal.exists()

    # The transaction returned success, so ordinary writes are allowed to move
    # both a journal target and the full index beyond the committed snapshot.
    sessions[0].messages.append(
        {"role": "assistant", "content": "post-archive continuation"}
    )
    sessions[0].save(touch_updated_at=False)
    unrelated = Session(
        session_id="unrelated-session",
        title="unrelated-session",
        workspace="",
        model="test-model",
        profile="default",
        messages=[{"role": "user", "content": "unrelated index update"}],
    )
    unrelated.save(touch_updated_at=False)
    root_path = lineage_session_store / "lineage-root.json"
    unrelated_path = lineage_session_store / "unrelated-session.json"
    index_path = lineage_session_store / "_index.json"
    post_commit_images = {
        "root": root_path.read_bytes(),
        "unrelated": unrelated_path.read_bytes(),
        "index": index_path.read_bytes(),
    }

    monkeypatch.setattr(transaction, "_remove_path", original_remove)
    run_startup_session_recovery(lineage_session_store)

    assert not journal.exists()
    assert root_path.read_bytes() == post_commit_images["root"]
    assert unrelated_path.read_bytes() == post_commit_images["unrelated"]
    assert index_path.read_bytes() == post_commit_images["index"]
    root = json.loads(root_path.read_text(encoding="utf-8"))
    index = {
        row["session_id"]: row
        for row in json.loads(index_path.read_text(encoding="utf-8"))
    }
    assert [message["content"] for message in root["messages"]] == [
        "lineage-root",
        "post-archive continuation",
    ]
    assert index["lineage-root"]["message_count"] == 2
    assert index["unrelated-session"]["message_count"] == 1
    _assert_cold_archive_parity(lineage_session_store, True)


def test_legacy_committed_recovery_journal_fails_closed_on_divergent_images(
    lineage_session_store,
):
    """A v1 recovery intent without preimages must not guess over newer bytes."""
    from api.models import Session
    import api.session_batch_transaction as transaction

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    root_path = lineage_session_store / "lineage-root.json"
    index_path = lineage_session_store / "_index.json"
    legacy_root = root_path.read_bytes()
    legacy_index = index_path.read_bytes()
    journal_path = lineage_session_store / transaction._JOURNAL_NAME
    journal_path.write_text(
        json.dumps(
            {
                "version": transaction._LEGACY_JOURNAL_VERSION,
                "transaction_id": "legacy-recovery-intent",
                "decision": "commit",
                "files": [
                    {"name": root_path.name, "new": transaction._encode(legacy_root)},
                    {"name": index_path.name, "new": transaction._encode(legacy_index)},
                ],
            }
        ),
        encoding="utf-8",
    )

    sessions[0].messages.append(
        {"role": "assistant", "content": "post-legacy continuation"}
    )
    sessions[0].save(touch_updated_at=False)
    unrelated = Session(
        session_id="unrelated-session",
        title="unrelated-session",
        workspace="",
        model="test-model",
        profile="default",
        messages=[{"role": "user", "content": "unrelated index update"}],
    )
    unrelated.save(touch_updated_at=False)
    newer_root = root_path.read_bytes()
    newer_index = index_path.read_bytes()

    result = transaction.recover_pending_session_batch(lineage_session_store)

    assert result["found"] is True
    assert result["recovered"] is False
    assert result["applied"] is False
    assert result["errors"] == [
        "lineage-root.json:RuntimeError",
        "_index.json:RuntimeError",
    ]
    assert journal_path.exists()
    assert root_path.read_bytes() == newer_root
    assert index_path.read_bytes() == newer_index


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


@pytest.mark.parametrize("target_archived", [True, False], ids=["archive", "restore"])
@pytest.mark.parametrize("damaged_live", ["missing", "malformed"])
def test_startup_backup_recovery_uses_index_metadata_when_live_sidecar_is_damaged(
    lineage_session_store,
    monkeypatch,
    target_archived,
    damaged_live,
):
    """A stale backup may rescue transcript data, never its metadata envelope."""
    import api.models as models
    import api.session_batch_transaction as transaction
    from api.session_recovery import run_startup_session_recovery

    sessions = _lineage_sessions(
        lineage_session_store,
        archived=not target_archived,
    )
    root = sessions[0]
    root.title = "current root title"
    root.project_id = "current-project"
    root.parent_session_id = "current-lineage-parent"
    root.pre_compression_snapshot = True
    root.source_tag = "webui"
    root.raw_source = "webui"
    root.session_source = "webui"
    root.source_label = "WebUI"
    root.user_id = "current-owner"
    root.save(touch_updated_at=False)

    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup_payload = json.loads(root_path.read_text(encoding="utf-8"))
    backup_payload.update(
        {
            "archived": not target_archived,
            "title": "stale backup title",
            "project_id": "stale-project",
            "profile": "stale-profile",
            "parent_session_id": "stale-lineage-parent",
            "pre_compression_snapshot": False,
            "source_tag": "stale-owner-source",
            "raw_source": "stale-owner-source",
            "session_source": "stale-owner-source",
            "source_label": "Stale owner",
            "user_id": "stale-owner",
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
    backup_path.write_text(
        json.dumps(backup_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    transaction.commit_session_archive_batch(sessions, target_archived)
    assert not (lineage_session_store / transaction._JOURNAL_NAME).exists()
    if damaged_live == "missing":
        root_path.unlink()
    else:
        root_path.write_text("{malformed live sidecar", encoding="utf-8")

    # Missing-sidecar discovery consults state.db only as a deletion/tombstone
    # check. Keep this regression isolated from the developer's real profile DB.
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    run_startup_session_recovery(lineage_session_store)

    recovered = json.loads(root_path.read_text(encoding="utf-8"))
    tip = json.loads(
        (lineage_session_store / "lineage-tip.json").read_text(encoding="utf-8")
    )
    assert recovered["archived"] is target_archived
    assert tip["archived"] is target_archived
    assert recovered["title"] == "current root title"
    assert recovered["project_id"] == "current-project"
    assert recovered["profile"] == "default"
    assert recovered["parent_session_id"] == "current-lineage-parent"
    assert recovered["pre_compression_snapshot"] is True
    assert recovered["source_tag"] == "webui"
    assert recovered["raw_source"] == "webui"
    assert recovered["session_source"] == "webui"
    assert recovered["source_label"] == "WebUI"
    assert recovered.get("user_id") != "stale-owner"
    assert [message["content"] for message in recovered["messages"]] == [
        "rescued prompt",
        "rescued answer",
    ]
    assert recovered["context_messages"] == backup_payload["context_messages"]
    _assert_cold_archive_parity(lineage_session_store, target_archived)


def test_backup_recovery_without_current_metadata_authority_leaves_explicit_residual(
    lineage_session_store,
):
    from api.session_recovery import recover_all_sessions_on_startup

    _lineage_sessions(lineage_session_store, archived=False)
    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup = json.loads(root_path.read_text(encoding="utf-8"))
    backup["messages"] = [
        {"role": "user", "content": "rescued prompt"},
        {"role": "assistant", "content": "rescued answer"},
    ]
    backup["parent_session_id"] = "stale-lineage-parent"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    root_path.unlink()
    (lineage_session_store / "_index.json").unlink()

    result = recover_all_sessions_on_startup(
        lineage_session_store,
        state_db_path=None,
    )

    assert result["restored"] == 0
    assert not root_path.exists()
    assert result["details"] == [
        {
            "session_id": "lineage-root",
            "live_messages": -1,
            "bak_messages": 2,
            "recommend": "restore",
            "restored": False,
            "recovery_residual": "metadata_authority_unavailable",
        }
    ]


@pytest.mark.parametrize("damaged_live", ["missing", "malformed"])
@pytest.mark.parametrize("index_state", ["complete", "incomplete", "absent"])
def test_ordinary_backup_metadata_requires_complete_current_index_authority(
    lineage_session_store,
    damaged_live,
    index_state,
):
    from api.session_recovery import recover_all_sessions_on_startup
    from api.models import Session

    live_path = lineage_session_store / "ordinary-session.json"
    backup_path = live_path.with_suffix(".json.bak")
    current = Session(
        session_id="ordinary-session",
        title="current title",
        workspace="/current/workspace",
        model="current-model",
        profile="current-profile",
        project_id="current-project",
        messages=[{"role": "user", "content": "current short transcript"}],
    )
    current.source_tag = "current-source"
    current.raw_source = "current-source"
    current.session_source = "current-source"
    current.source_label = "Current source"
    current.save(touch_updated_at=False)

    ordinary_backup = json.loads(live_path.read_text(encoding="utf-8"))
    ordinary_backup.update(
        {
            "title": "stale title",
            "workspace": "/stale/workspace",
            "model": "stale-model",
            "profile": "stale-profile",
            "project_id": "stale-project",
            "source_tag": "stale-source",
            "raw_source": "stale-source",
            "session_source": "stale-source",
            "source_label": "Stale source",
            "messages": [
                {"role": "user", "content": "ordinary rescue"},
                {"role": "assistant", "content": "rescued answer"},
            ],
            "context_messages": [
                {"role": "user", "content": "ordinary rescue"},
                {"role": "assistant", "content": "rescued answer"},
            ],
        }
    )
    backup_path.write_text(json.dumps(ordinary_backup), encoding="utf-8")
    index_path = lineage_session_store / "_index.json"
    if index_state == "incomplete":
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index[0].pop("model")
        index_path.write_text(json.dumps(index), encoding="utf-8")
    elif index_state == "absent":
        index_path.unlink()
    if damaged_live == "missing":
        live_path.unlink()
        damaged_bytes = None
    else:
        live_path.write_text("{malformed live sidecar", encoding="utf-8")
        damaged_bytes = live_path.read_bytes()

    result = recover_all_sessions_on_startup(
        lineage_session_store,
        state_db_path=None,
    )

    if index_state == "complete":
        assert result["restored"] == 1
        recovered = json.loads(live_path.read_text(encoding="utf-8"))
        assert recovered["title"] == "current title"
        assert recovered["workspace"] == "/current/workspace"
        assert recovered["model"] == "current-model"
        assert recovered["profile"] == "current-profile"
        assert recovered["project_id"] == "current-project"
        assert recovered["source_tag"] == "current-source"
        assert recovered["raw_source"] == "current-source"
        assert recovered["session_source"] == "current-source"
        assert recovered["source_label"] == "Current source"
        assert [message["content"] for message in recovered["messages"]] == [
            "ordinary rescue",
            "rescued answer",
        ]
    else:
        assert result["restored"] == 0
        assert result["details"][0]["recovery_residual"] == "metadata_authority_unavailable"
        if damaged_bytes is None:
            assert not live_path.exists()
        else:
            assert live_path.read_bytes() == damaged_bytes


@pytest.mark.parametrize("target_archived", [True, False], ids=["archive", "restore"])
def test_backup_recovery_crash_after_sidecar_replace_reconciles_index_on_fresh_startup(
    lineage_session_store,
    monkeypatch,
    target_archived,
):
    """A durable recovery intent closes the sidecar/index crash window."""
    import api.models as models
    import api.session_batch_transaction as transaction
    import api.session_recovery as session_recovery

    sessions = _lineage_sessions(
        lineage_session_store,
        archived=not target_archived,
    )
    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup = json.loads(root_path.read_text(encoding="utf-8"))
    backup.update(
        {
            "archived": not target_archived,
            "title": "stale backup title",
            "profile": "stale-profile",
            "project_id": "stale-project",
            "source_tag": "stale-source",
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
    backup_path.write_text(json.dumps(backup), encoding="utf-8")

    transaction.commit_session_archive_batch(sessions, target_archived)
    root_path.unlink()

    class SimulatedCrash(BaseException):
        pass

    original_replace = os.replace
    crashed = False

    def crash_after_recovered_sidecar_replace(src, dst):
        nonlocal crashed
        result = original_replace(src, dst)
        if Path(dst) == root_path and not crashed:
            crashed = True
            raise SimulatedCrash("injected crash after recovered sidecar replacement")
        return result

    monkeypatch.setattr(session_recovery.os, "replace", crash_after_recovered_sidecar_replace)
    with pytest.raises(SimulatedCrash):
        session_recovery.recover_session(root_path)

    journal_path = lineage_session_store / transaction._JOURNAL_NAME
    assert journal_path.exists()
    assert len(json.loads(root_path.read_text(encoding="utf-8"))["messages"]) == 2
    stale_index = {
        row["session_id"]: row
        for row in json.loads(
            (lineage_session_store / "_index.json").read_text(encoding="utf-8")
        )
    }
    assert stale_index["lineage-root"]["message_count"] == 1

    # Simulate a fresh process. Durable replay must reconcile the index before
    # ordinary .bak scanning observes equal live/backup transcript lengths.
    monkeypatch.setattr(session_recovery.os, "replace", original_replace)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    ordinary_recovery_results = []
    real_recover_session = session_recovery.recover_session

    def track_ordinary_recovery(path):
        result = real_recover_session(path)
        ordinary_recovery_results.append(result)
        return result

    monkeypatch.setattr(session_recovery, "recover_session", track_ordinary_recovery)
    session_recovery.run_startup_session_recovery(lineage_session_store)

    assert not journal_path.exists()
    assert ordinary_recovery_results
    assert all(result["restored"] is False for result in ordinary_recovery_results)
    recovered = json.loads(root_path.read_text(encoding="utf-8"))
    index = {
        row["session_id"]: row
        for row in json.loads(
            (lineage_session_store / "_index.json").read_text(encoding="utf-8")
        )
    }
    assert recovered["archived"] is target_archived
    assert recovered["title"] == "lineage-root"
    assert recovered["profile"] == "default"
    assert recovered.get("project_id") != "stale-project"
    assert recovered.get("source_tag") != "stale-source"
    assert index["lineage-root"]["archived"] is target_archived
    assert index["lineage-root"]["message_count"] == 2
    _assert_cold_archive_parity(lineage_session_store, target_archived)


def test_published_backup_recovery_journal_never_replays_over_later_writes(
    lineage_session_store,
    monkeypatch,
):
    """A recovered transcript and unrelated index update survive a later restart."""
    from api.models import Session, _load_session_from_path
    import api.session_batch_transaction as transaction
    import api.session_recovery as session_recovery

    _lineage_sessions(lineage_session_store, archived=False)
    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup = json.loads(root_path.read_text(encoding="utf-8"))
    backup["messages"] = [
        {"role": "user", "content": "lineage-root"},
        {"role": "assistant", "content": "rescued answer"},
    ]
    backup["context_messages"] = list(backup["messages"])
    backup_path.write_text(json.dumps(backup), encoding="utf-8")

    original_remove = transaction._remove_path

    def leave_committed_journal(path):
        if path.name == transaction._JOURNAL_NAME:
            raise OSError("injected journal cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(transaction, "_remove_path", leave_committed_journal)
    result = session_recovery.recover_session(root_path)
    journal = lineage_session_store / transaction._JOURNAL_NAME
    assert result["restored"] is True
    assert journal.exists()

    continued = _load_session_from_path(root_path)
    assert continued is not None
    continued._metadata_message_count = None
    continued.messages.append(
        {"role": "assistant", "content": "post-recovery continuation"}
    )
    continued.save(touch_updated_at=False)
    unrelated = Session(
        session_id="unrelated-session",
        title="unrelated-session",
        workspace="",
        model="test-model",
        profile="default",
        messages=[{"role": "user", "content": "unrelated index update"}],
    )
    unrelated.save(touch_updated_at=False)
    unrelated_path = lineage_session_store / "unrelated-session.json"
    index_path = lineage_session_store / "_index.json"
    post_recovery_images = {
        "root": root_path.read_bytes(),
        "unrelated": unrelated_path.read_bytes(),
        "index": index_path.read_bytes(),
    }

    monkeypatch.setattr(transaction, "_remove_path", original_remove)
    session_recovery.run_startup_session_recovery(lineage_session_store)

    assert not journal.exists()
    assert root_path.read_bytes() == post_recovery_images["root"]
    assert unrelated_path.read_bytes() == post_recovery_images["unrelated"]
    assert index_path.read_bytes() == post_recovery_images["index"]
    recovered = json.loads(root_path.read_text(encoding="utf-8"))
    index = {
        row["session_id"]: row
        for row in json.loads(index_path.read_text(encoding="utf-8"))
    }
    assert [message["content"] for message in recovered["messages"]] == [
        "lineage-root",
        "rescued answer",
        "post-recovery continuation",
    ]
    assert index["lineage-root"]["message_count"] == 3
    assert index["unrelated-session"]["message_count"] == 1


def test_backup_recovery_serializes_with_lineage_commit_before_sidecar_replace(
    lineage_session_store,
    monkeypatch,
):
    """Recovery staged before a commit cannot publish stale metadata afterward."""
    import api.session_batch_transaction as transaction
    import api.session_recovery as session_recovery

    sessions = _lineage_sessions(lineage_session_store, archived=False)
    root_path = lineage_session_store / "lineage-root.json"
    backup_path = root_path.with_suffix(".json.bak")
    backup = json.loads(root_path.read_text(encoding="utf-8"))
    backup["messages"] = [
        {"role": "user", "content": "rescued prompt"},
        {"role": "assistant", "content": "rescued answer"},
    ]
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    # The transaction's staged object already carries the transcript that the
    # recovery thread will publish, isolating this regression to metadata order.
    sessions[0].messages = list(backup["messages"])

    recovery_staged = threading.Event()
    allow_recovery_replace = threading.Event()
    commit_finished = threading.Event()
    recovery_results = []
    thread_errors = []
    original_replace = transaction._replace_bytes

    def pause_recovery_replace(path, payload):
        if path == root_path and not recovery_staged.is_set():
            recovery_staged.set()
            if not allow_recovery_replace.wait(timeout=10):
                raise TimeoutError("test did not release recovery publication")
        return original_replace(path, payload)

    monkeypatch.setattr(transaction, "_replace_bytes", pause_recovery_replace)

    def run_recovery():
        try:
            recovery_results.append(session_recovery.recover_session(root_path))
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)

    def run_commit():
        try:
            transaction.commit_session_archive_batch(sessions, True)
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)
        finally:
            commit_finished.set()

    recovery_thread = threading.Thread(target=run_recovery)
    recovery_thread.start()
    assert recovery_staged.wait(timeout=10)
    commit_thread = threading.Thread(target=run_commit)
    commit_thread.start()
    commit_was_blocked = not commit_finished.wait(timeout=0.5)
    allow_recovery_replace.set()
    recovery_thread.join(timeout=10)
    commit_thread.join(timeout=10)

    assert commit_was_blocked, "lineage commit passed recovery's store authority lock"
    assert not recovery_thread.is_alive()
    assert not commit_thread.is_alive()
    assert thread_errors == []
    assert recovery_results[0]["restored"] is True
    _assert_cold_archive_parity(lineage_session_store, True)


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
    assert "const payload={session_id:sessionId,archived,lineage:true}" in SESSIONS_JS
    assert "const response=await _requestSessionArchive(session.session_id,archived);" in SESSIONS_JS
    assert "Promise.all(targets.map" not in SESSIONS_JS
    script = f"""
const src={SESSIONS_JS!r};
function extract(name){{
  let start=src.indexOf(`function ${{name}}(`);
  if(src.slice(start-6,start)==='async ') start-=6;
  let brace=src.indexOf('{{',start), depth=0, end=-1;
  for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
  return src.slice(start,end);
}}
eval(extract('_requestSessionArchive'));
eval(extract('_archiveSession'));
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
function extract(name){{
  let start=src.indexOf(`function ${{name}}(`);
  if(src.slice(start-6,start)==='async ') start-=6;
  let brace=src.indexOf('{{',start), depth=0, end=-1;
  for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
  return src.slice(start,end);
}}
eval(extract('_requestSessionArchive'));
eval(extract('_archiveSession'));
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


def test_batch_archive_delegates_collapsed_lineage_scope_to_backend():
    """Batch selection must not bypass the lineage-aware archive authority."""
    assert "const response=await _requestSessionArchive(sid,true);" in SESSIONS_JS
    script = f"""
const src={SESSIONS_JS!r};
function extract(name){{
  let start=src.indexOf(`function ${{name}}(`);
  if(src.slice(start-6,start)==='async ') start-=6;
  let brace=src.indexOf('{{',start), depth=0, end=-1;
  for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
  return src.slice(start,end);
}}
eval(extract('_requestSessionArchive'));
eval(extract('_renderBatchActionBar'));
const calls=[];
const bar={{innerHTML:'',style:{{}},children:[],appendChild(el){{this.children.push(el);}},querySelectorAll:()=>[]}};
Object.assign(globalThis,{{
  $:id=>id==='batchActionBar'?bar:null,
  document:{{
    createElement:()=>({{className:'',textContent:'',onclick:null,style:{{}},children:[],appendChild(el){{this.children.push(el);}}}}),
    querySelectorAll:()=>[],addEventListener:()=>{{}},removeEventListener:()=>{{}}
  }},
  _selectedSessions:new Set(['collapsed-root']),t:key=>key,_worktreeSessionCount:()=>0,
  _sessionSnapshotById:sid=>({{session_id:sid}}),showConfirmDialog:async()=>true,
  api:async(p,o)=>{{calls.push({{path:p,body:JSON.parse(o.body)}});return {{session_ids:['collapsed-root','hidden-tip']}};}},
  _worktreeResponseCount:()=>0,showToast:()=>{{}},exitSessionSelectMode:()=>{{}},renderSessionList:async()=>{{}},
  _showBatchProjectPicker:()=>{{}},_clearHandoffStorageForSession:()=>{{}},
  S:{{session:null,messages:[],entries:[]}},localStorage:{{removeItem:()=>{{}}}},
  _hydrateTodosFromSession:()=>{{}},_sessionListQueryString:()=>''
}});
_renderBatchActionBar();
bar.children[1].onclick().then(()=>console.log(JSON.stringify(calls)));
"""
    proc = subprocess.run(["node"], input=script, text=True, capture_output=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        {
            "path": "/api/session/archive",
            "body": {
                "session_id": "collapsed-root",
                "archived": True,
                "lineage": True,
            },
        }
    ]
