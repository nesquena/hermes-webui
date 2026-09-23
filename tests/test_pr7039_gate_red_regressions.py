"""Régressions des deux must-fix du gate certification RED du 09/09/2026 (PR #7039).

Finding 1 (CORE/liveness) — /btw cancellation self-deadlock:
``_cleanup_ephemeral_session_sidecar_locked`` ré-acquérait le
``threading.Lock`` non-réentrant ``_get_session_agent_lock(sid)`` alors que
tous les callers production le détiennent déjà (cancel via
``_finalize_cancelled_turn`` sous ``with _agent_lock:``, completion normale)
→ deadlock du worker, session ephemeral persistante sur disque/sidebar.

Fix: le helper exige que le lock soit détenu par le caller (il ne
l'acquiert plus), et le caller de completion l'acquiert explicitement.

Finding 2 (SILENT/data-loss) — hidden-cleanup churn évince les fences:
le cleanup hidden /btw/background enregistrait ses tombstones dans le log
unique partagé (cap 1000) des suppressions utilisateur; après >1000 hidden
cleanups une fence utilisateur était évincée et
``recover_missing_sidecars_from_state_db`` re-matérialisait le transcript
supprimé (quand la ligne state.db avait survécu).

Fix: les cleanups hidden enregistrent dans leur PROPRE log borné
(``_hidden_cleanup_sessions.json``, cap indépendant) — aucune capacité
partagée avec les fences utilisateur; les readers OR-ent les deux logs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace


def _patch_store(monkeypatch, tmp_path):
    from api import models, routes, streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    with models.LOCK:
        models.SESSIONS.clear()
    return session_dir


def _invoke_delete_route(monkeypatch, routes, sid):
    from api import models

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: (
            captured.update(payload=payload, status=status) or True
        ),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400, **_kwargs: (
            captured.update(payload={"error": message}, status=status) or True
        ),
    )
    assert routes.handle_post(
        object(), SimpleNamespace(path="/api/session/delete")
    ) is True
    return captured


# ────────────────────────────────────────────────────────────────────────────
# Finding 1 — /btw cancellation self-deadlock (production-shaped)
# ────────────────────────────────────────────────────────────────────────────


def test_ephemeral_cancel_worker_completes_and_sidecar_removed(tmp_path, monkeypatch):
    """Production shape: the worker holds the per-session agent lock when the
    ephemeral cancel cleanup runs. The worker MUST complete and the sidecar
    MUST be gone — before the fix this self-deadlocked forever."""
    from api import models, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "btw-cancel-deadlock"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "side question"}],
    )
    session.active_stream_id = "stream-1"
    session.pending_user_message = "side question"
    session.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    assert sidecar.exists()

    errors: list = []
    finished = threading.Event()

    # Production admission shape: the route layer registers the writeback
    # owner next to active_stream_id before the worker starts (see /btw and
    # /api/chat/start handlers).
    from api.config import register_session_writeback_owner

    register_session_writeback_owner(sid, "stream-1")

    def _worker():
        # The production cancel path runs the ephemeral cleanup while holding
        # the per-session agent lock (all _finalize_cancelled_turn callers do).
        agent_lock = streaming._get_session_agent_lock(sid)
        try:
            with agent_lock:
                streaming._finalize_cancelled_turn(
                    session, ephemeral=True, stream_id="stream-1"
                )
            finished.set()
        except BaseException as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)
            finished.set()

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    hung = not finished.wait(timeout=10.0)
    worker.join(timeout=5.0)

    assert not hung, (
        "FINDING 1: le worker ephemeral cancel deadlock — "
        "_cleanup_ephemeral_session_sidecar_locked ré-acquiert le "
        "_get_session_agent_lock non-réentrant déjà détenu par le caller."
    )
    assert not errors, f"worker raised: {errors}"
    assert not sidecar.exists(), (
        "le sidecar ephemeral doit être supprimé après un cancel propre"
    )


def test_ephemeral_completion_cleanup_holds_agent_lock_and_removes_sidecar(
    tmp_path, monkeypatch
):
    """Production shape: the completion path (``outcome='completed'``) acquires
    the per-session agent lock around the cleanup — the helper itself must not
    re-acquire it (self-deadlock) and must not run naked either."""
    from api import models, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "btw-complete-cleanup"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "side question"}],
    )
    session.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    assert sidecar.exists()

    agent_lock = streaming._get_session_agent_lock(sid)
    # Mirror the completion path's explicit acquisition (streaming.py ~11355).
    with agent_lock:
        streaming._cleanup_ephemeral_session_sidecar_locked(
            session, outcome="completed"
        )

    assert not sidecar.exists()
    # The hidden fence must exist in the SEPARATE hidden log, not the user log.
    assert sid in models._load_webui_hidden_cleanup_tombstone()
    assert sid not in models._load_webui_deleted_session_tombstone()


def test_ephemeral_cancel_cleanup_records_hidden_fence_not_user_fence(
    tmp_path, monkeypatch
):
    """The hidden /btw cleanup fences in its own log (finding 2 wiring)."""
    from api import models, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "btw-fence-kind"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "side question"}],
    )
    session.save(skip_index=True)

    agent_lock = streaming._get_session_agent_lock(sid)
    with agent_lock:
        streaming._cleanup_ephemeral_cancelled_turn(session)

    assert not (session_dir / f"{sid}.json").exists()
    assert sid in models._load_webui_hidden_cleanup_tombstone()
    assert sid not in models._load_webui_deleted_session_tombstone()
    # The union reader sees the hidden fence as a delete fence.
    assert models._webui_deleted_session_is_tombstoned(sid)


# ────────────────────────────────────────────────────────────────────────────
# Finding 2 — hidden-cleanup churn évince les fences utilisateur
# ────────────────────────────────────────────────────────────────────────────


def test_hidden_cleanup_churn_never_evicts_user_delete_fence(tmp_path, monkeypatch):
    """Production shape: a durable user delete fence, then >CAP hidden
    /btw cleanups. The user fence MUST survive (own-capacity logs) and
    repair-safe MUST NOT re-materialize the deleted transcript from a
    surviving state.db row."""
    from api import models, routes, session_recovery, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)

    # 1) Durable user delete of the target (route-shaped, fence kind=user).
    target_sid = "user-deleted-target"
    user_session = models.Session(
        session_id=target_sid,
        messages=[
            {"role": "user", "content": "transcript supprime par l utilisateur"}
        ],
    )
    user_session.save(skip_index=True)
    target_sidecar = session_dir / f"{target_sid}.json"
    captured = _invoke_delete_route(monkeypatch, routes, target_sid)
    assert captured["status"] == 200, captured
    assert not target_sidecar.exists()
    assert models._webui_deleted_session_is_tombstoned(target_sid)
    assert target_sid in models._load_webui_deleted_session_tombstone()

    # 2) The state.db row for the target survived (its cleanup failed before).
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, 'webui', 'deleted', 'model', 1, 1)",
            (target_sid,),
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, ?, 'user', 'deleted', 1)",
            (target_sid,),
        )

    # 3) >CAP hidden /btw cleanups — they must all fence into the hidden log.
    cap = models.WEBUI_DELETED_SESSION_TOMBSTONE_CAP
    hidden_cap = models.WEBUI_HIDDEN_CLEANUP_TOMBSTONE_CAP
    churn = max(cap, hidden_cap) + 1
    for index in range(churn):
        hidden_sid = f"btw-hidden-{index:06d}"
        hidden_session = models.Session(
            session_id=hidden_sid,
            messages=[{"role": "user", "content": f"ephemeral {index}"}],
        )
        hidden_session.save(skip_index=True)
        agent_lock = streaming._get_session_agent_lock(hidden_sid)
        with agent_lock:
            streaming._cleanup_ephemeral_cancelled_turn(hidden_session)

    # The user fence survived the hidden churn (separate capacity).
    assert target_sid in models._load_webui_deleted_session_tombstone(), (
        "FINDING 2: la fence utilisateur a été évincée par le hidden-cleanup churn"
    )
    assert models._webui_deleted_session_is_tombstoned(target_sid)
    # The hidden log is bounded independently.
    hidden_ids = models._load_webui_hidden_cleanup_tombstone_ids()
    assert len(hidden_ids) <= models.WEBUI_HIDDEN_CLEANUP_TOMBSTONE_CAP
    assert hidden_ids[-1] == f"btw-hidden-{churn - 1:06d}"

    # 4) repair-safe does NOT resurrect the user-deleted transcript.
    result = session_recovery.recover_missing_sidecars_from_state_db(
        session_dir, db_path
    )
    materialized_ids = {
        detail.get("session_id")
        for detail in result["details"]
        if detail.get("materialized")
    }
    assert target_sid not in materialized_ids, (
        "FINDING 2: le transcript utilisateur supprimé a été re-matérialisé"
    )
    assert not target_sidecar.exists()
    # And the recovery path also refuses hidden-cleaned sids (their own
    # anti-resurrection guarantee): seed one such row into state.db.
    hidden_survivor = hidden_ids[0]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?, 'webui', 'hidden', 'model', 2, 1)",
            (hidden_survivor,),
        )
        connection.execute(
            "INSERT INTO messages VALUES (2, ?, 'user', 'hidden cleanup survivor', 1)",
            (hidden_survivor,),
        )
    result2 = session_recovery.recover_missing_sidecars_from_state_db(
        session_dir, db_path
    )
    materialized2 = {
        detail.get("session_id")
        for detail in result2["details"]
        if detail.get("materialized")
    }
    assert hidden_survivor not in materialized2, (
        "un sid hidden-cleaned ne doit pas être re-matérialisé par repair-safe"
    )
    assert not (session_dir / f"{hidden_survivor}.json").exists()


def test_user_and_hidden_fences_are_independent_logs(tmp_path, monkeypatch):
    """Structural control: the two fences are separate files with separate
    capacity; user deletes never touch the hidden log and vice versa."""
    from api import models, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)

    user_sid = "user-delete-plain"
    s = models.Session(
        session_id=user_sid, messages=[{"role": "user", "content": "x"}]
    )
    s.save(skip_index=True)
    with streaming._get_session_agent_lock(user_sid):
        with models._session_sidecar_authority(user_sid):
            models._delete_session_sidecar_artifacts_locked(user_sid)
    assert user_sid in models._load_webui_deleted_session_tombstone()
    assert user_sid not in models._load_webui_hidden_cleanup_tombstone()
    assert not models._webui_hidden_cleanup_tombstone_file().exists()

    hidden_sid = "hidden-cleanup-plain"
    hs = models.Session(
        session_id=hidden_sid, messages=[{"role": "user", "content": "y"}]
    )
    hs.save(skip_index=True)
    with streaming._get_session_agent_lock(hidden_sid):
        with models._session_sidecar_authority(hidden_sid):
            models._delete_session_sidecar_artifacts_locked(
                hidden_sid, tombstone_kind="hidden"
            )
    assert hidden_sid in models._load_webui_hidden_cleanup_tombstone()
    assert hidden_sid not in models._load_webui_deleted_session_tombstone()

    # Independent caps, separate files, union reader sees both.
    assert (
        models._webui_deleted_session_tombstone_file()
        != models._webui_hidden_cleanup_tombstone_file()
    )
    assert models._webui_deleted_session_is_tombstoned(user_sid)
    assert models._webui_deleted_session_is_tombstoned(hidden_sid)
    assert (session_dir / "_hidden_cleanup_sessions.json").exists()


def test_session_save_clears_hidden_fence_for_recreated_sid(tmp_path, monkeypatch):
    """A live save with real messages clears the hidden fence too — a
    re-created/re-imported sid must not stay shadowed (parity with the user
    fence semantics)."""
    from api import models, streaming

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "btw-recreated"
    s = models.Session(
        session_id=sid, messages=[{"role": "user", "content": "first"}]
    )
    s.save(skip_index=True)
    with streaming._get_session_agent_lock(sid):
        streaming._cleanup_ephemeral_cancelled_turn(s)
    assert sid in models._load_webui_hidden_cleanup_tombstone()

    # Same sid explicitly re-created and saved with a real message.
    reborn = models.Session(
        session_id=sid, messages=[{"role": "user", "content": "reborn"}]
    )
    reborn.save(skip_index=True)

    assert sid not in models._load_webui_hidden_cleanup_tombstone()
    assert (session_dir / f"{sid}.json").exists()
    assert models._webui_deleted_session_is_tombstoned(sid) is False


def test_background_hidden_delete_fences_hidden_log(tmp_path, monkeypatch):
    """The hidden background route cleanup fences into the hidden log."""
    from api import models, routes

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "bg-hidden-delete"
    s = models.Session(
        session_id=sid, messages=[{"role": "user", "content": "bg"}]
    )
    s.save(skip_index=True)

    deleted = routes._delete_hidden_background_session_sidecar(sid)
    assert deleted is True
    assert not (session_dir / f"{sid}.json").exists()
    assert sid in models._load_webui_hidden_cleanup_tombstone()
    assert sid not in models._load_webui_deleted_session_tombstone()
    assert models._webui_deleted_session_is_tombstoned(sid)


def _run_background_route(monkeypatch, tmp_path, cleanup):
    """Drive ``_handle_background`` with a stubbed agent run and a patched
    hidden-cleanup helper; return the answer published to the parent task."""
    from api import background, models, routes

    _patch_store(monkeypatch, tmp_path)
    parent = models.Session(
        session_id="bg-parent", messages=[{"role": "user", "content": "hi"}]
    )
    parent.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[parent.session_id] = parent
    background._BACKGROUND_TASKS.clear()

    worker_done = threading.Event()
    real_thread = threading.Thread

    class _Thread(real_thread):
        def run(self):
            try:
                super().run()
            finally:
                worker_done.set()

    monkeypatch.setattr(routes.threading, "Thread", _Thread)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)

    def _fake_run(bg_sid, *_args, **_kwargs):
        bg = models.Session.load(bg_sid)
        assert bg is not None
        bg.messages.append({"role": "assistant", "content": "real answer"})
        bg.save(skip_index=True)

    monkeypatch.setattr(routes, "_run_agent_streaming", _fake_run)
    monkeypatch.setattr(
        routes, "_delete_hidden_background_session_sidecar", cleanup
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: payload,
    )
    response = routes._handle_background(
        object(), {"session_id": parent.session_id, "prompt": "do it"}
    )
    assert isinstance(response, dict)
    assert worker_done.wait(10), "background worker did not finish"
    with routes.STREAMS_LOCK:
        routes.STREAMS.pop(response["stream_id"], None)
    results = background.get_results(parent.session_id)
    assert len(results) == 1
    assert results[0]["task_id"] == response["task_id"]
    return results[0]["answer"]


def test_background_cleanup_false_result_is_reported_as_failure(
    tmp_path, monkeypatch
):
    """Greptile P1: a ``False`` return from the hidden cleanup (sidecar
    revision changed under the lock) must follow the same failure path as an
    exception — never publish the assistant answer as a success."""
    answer = _run_background_route(monkeypatch, tmp_path, lambda _sid: False)
    assert answer == "(background task cleanup failed)"


def test_background_cleanup_exception_is_reported_as_failure(
    tmp_path, monkeypatch
):
    def _boom(_sid):
        raise RuntimeError("unlink failed")

    answer = _run_background_route(monkeypatch, tmp_path, _boom)
    assert answer == "(background task cleanup failed)"


def test_background_cleanup_true_result_publishes_answer(tmp_path, monkeypatch):
    answer = _run_background_route(monkeypatch, tmp_path, lambda _sid: True)
    assert answer == "real answer"


def test_recovery_reader_honors_hidden_fence_fallback(tmp_path, monkeypatch):
    """session_recovery's direct-file fallback ORs the hidden log (used when
    the caller's session_dir differs from models.SESSION_DIR)."""
    from api import models, session_recovery

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "fallback-hidden"
    models._record_webui_hidden_cleanup_tombstone(sid)

    # Direct-file path: the sid has no live sidecar, both logs on disk.
    assert session_recovery._durable_tombstone_marks_deleted_webui_session(
        session_dir, sid
    )
    # Union must also hold through models.SESSION_DIR-equal path.
    assert models._webui_deleted_session_is_tombstoned(sid)


def test_compactor_refuses_hidden_fenced_publication(tmp_path, monkeypatch):
    """The compactor's tombstone refusal honors the hidden fence as well."""
    from api import models
    from scripts import compact_session_replays as compactor

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "compactor-hidden-fence"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "id": "assistant-a",
                        "finish_reason": "stop",
                        "reasoning": "recoverable private transcript",
                        "timestamp": 123,
                    }
                    for _ in range(2)
                ],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )
    compacted = compactor.compact_sidecar(sidecar)
    models._record_webui_hidden_cleanup_tombstone(sid)

    import pytest

    with pytest.raises(compactor.StreamJSONError, match="deleted|tombstone"):
        compactor.restore_manifest(Path(compacted["manifest"]))
