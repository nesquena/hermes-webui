"""Coverage for api/context_brief.py — the read-only session context brief
behind the WebUI Context panel (POST /api/session/context-brief,
POST .../context-brief/refresh, GET .../context-brief/status).

Exercised with fake Session objects and a real sidecar dir in tmp_path:
deterministic assembly (requests, conclusions, compressions, todos, goal,
in-flight), LLM brief persistence + staleness, job lifecycle (start, 409 on
duplicate, status polling, fallback when the auxiliary model is absent).
"""

import json
import time
from pathlib import Path  # noqa: F401  (kept for fixture parity with the auto layer)
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api import context_brief


SID = "20260812_120000_cb12ef"


def _make_session(tmp_path, *, messages=None, **overrides):
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{SID}.json"
    if messages is None:
        messages = [
            {"role": "user", "content": "fais le point sur le chantier", "timestamp": 1000.0},
            {"role": "assistant", "content": "je regarde…", "timestamp": 1000.5},
            {
                "role": "assistant",
                "content": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: lot 1 livré et vérifié",
                "timestamp": 1001.0,
            },
            {"role": "user", "content": "maintenant déploie", "timestamp": 1002.0},
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "todos": [
                            {"id": "1", "content": "livrer le lot 1", "status": "completed"},
                            {"id": "2", "content": "déployer en production", "status": "in_progress"},
                            {"id": "3", "content": "nettoyer la worktree", "status": "pending"},
                        ]
                    }
                ),
                "timestamp": 1002.5,
            },
            {"role": "assistant", "content": "[[SILENT]]", "timestamp": 1002.6},
            {
                "role": "assistant",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\n ancien résumé",
                "timestamp": 1002.7,
            },
            {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 déploiement vérifié sur le live", "timestamp": 1003.0},
            {
                "role": "user",
                "content": "[IMPORTANT: Background process x completed]",
                "timestamp": 1003.1,
                "_source": "process_wakeup",
            },
        ]
    sess = SimpleNamespace(
        session_id=SID,
        title="chantier contexte",
        workspace="/tmp/ws",
        model="k3-256k",
        created_at=1000.0,
        updated_at=1003.0,
        messages=messages,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        pending_turn_id=None,
        pending_attachments=[],
        profile="default",
        path=path,
    )
    for key, value in overrides.items():
        setattr(sess, key, value)
    return sess


@pytest.fixture(autouse=True)
def _isolate_jobs():
    with context_brief._JOBS_LOCK:
        context_brief._JOBS.clear()
    yield
    with context_brief._JOBS_LOCK:
        threads = []
        for job in context_brief._JOBS.values():
            thread = job.get("_thread")
            if thread is not None:
                threads.append(thread)
    # Workers call _finish_job(), which needs _JOBS_LOCK. Never join while
    # holding that lock or the teardown itself strands a worker into the next
    # test file and lets it touch real api.models state after patches unwind.
    for thread in threads:
        thread.join(timeout=5)
    alive = [thread.name for thread in threads if thread.is_alive()]
    with context_brief._JOBS_LOCK:
        context_brief._JOBS.clear()
    assert not alive, f"context-brief worker(s) survived test teardown: {alive}"


def _patch_resolution(sess):
    from api import models

    return patch.object(models, "get_session", lambda sid, **kw: sess if sid == SID else (_ for _ in ()).throw(KeyError(sid)))


# ── deterministic assembly ───────────────────────────────────────────────

def test_deterministic_brief_assembles_all_blocks(tmp_path):
    sess = _make_session(tmp_path)
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    assert brief["session_id"] == SID
    assert brief["meta"]["title"] == "chantier contexte"
    assert brief["meta"]["message_count"] == len(sess.messages)
    assert brief["meta"]["source"] == "webui"

    # User requests: real demands only — the process_wakeup card is excluded.
    assert [r["text"] for r in brief["requests"]] == ["fais le point sur le chantier", "maintenant déploie"]
    assert brief["request_count"] == 2

    accomplished = brief["accomplished"]
    # Two CONCLUSION blocks, most recent kept; excerpts come from the 🟢 line.
    assert accomplished["conclusion_count"] == 2
    assert accomplished["conclusions"][-1]["excerpt"].startswith("🟢 déploiement vérifié")
    # The compaction marker is counted as a compression milestone.
    assert accomplished["compression_count"] == 1
    assert accomplished["compressions"][0]["kind"] == "compaction"
    # [[SILENT]] must never surface as the last assistant reply.
    assert accomplished["last_assistant"]["excerpt"].startswith("# CONCLUSION")

    # Todos from the latest tool snapshot.
    todos = brief["todos"]
    assert todos is not None
    assert todos["counts"]["completed"] == 1
    assert todos["counts"]["in_progress"] == 1
    assert todos["counts"]["pending"] == 1
    assert todos["current"] == "déployer en production"


def test_deterministic_brief_empty_session(tmp_path):
    sess = _make_session(tmp_path, messages=[])
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")
    assert brief["meta"]["message_count"] == 0
    assert brief["requests"] == []
    assert brief["todos"] is None
    assert brief["goal"] is None
    assert brief["accomplished"]["conclusion_count"] == 0


def test_requests_exclude_runtime_injected_user_messages(tmp_path):
    """Direction ask 2026-08-16: 'Your requests' = human asks only.

    Goal continuations, delegation batches, background-process wakeups and
    compaction handoffs all arrive with role='user' but are runtime
    plumbing. Older wakeups predate the `_source` marker, so the text
    prefix must be enough on its own.
    """
    ws = "[Workspace::v1: /a0/usr/projects/MES]\n"
    messages = [
        {"role": "user", "content": ws + "déploie la nouvelle version", "timestamp": 1.0},
        {
            "role": "user",
            "content": ws + "[Continuing toward your standing goal]\nGoal: /validation",
            "timestamp": 2.0,
        },
        {
            "role": "user",
            "content": ws + "[IMPORTANT: Background process proc_abc completed (exit_code=0).",
            "timestamp": 3.0,
        },
        {
            "role": "user",
            "content": "[ASYNC DELEGATION BATCH COMPLETE] 3 tasks finished",
            "timestamp": 4.0,
        },
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns…",
            "timestamp": 5.0,
        },
        {"role": "user", "content": ws + "vérifie le live", "timestamp": 6.0},
    ]
    sess = _make_session(tmp_path, messages=messages)
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    assert [r["text"] for r in brief["requests"]] == [
        "déploie la nouvelle version",
        "vérifie le live",
    ]
    assert brief["request_count"] == 2


def test_requests_strip_workspace_tag_and_dedupe(tmp_path):
    """The workspace tag is plumbing, and one ask must appear once.

    A single turn can be persisted more than once (api_content mirror,
    startup recovery replay, lineage/state.db merge). The merged copies carry
    drifted timestamps, so dedupe is by TEXT alone (user report 2026-08-18):
    identical asks collapse to one entry keeping the first timestamp.
    """
    ws = "[Workspace::v1: /a0/usr/projects/MES]\n"
    messages = [
        {"role": "user", "content": ws + "corrige le brief", "timestamp": 10.0},
        {"role": "user", "content": "corrige le brief", "timestamp": 10.0},
        {"role": "user", "content": ws + "corrige le brief", "timestamp": 11.0},
    ]
    sess = _make_session(tmp_path, messages=messages)
    brief = context_brief.build_deterministic_brief(sess, SID, source="webui")

    texts = [r["text"] for r in brief["requests"]]
    assert all(not t.startswith("[Workspace::v") for t in texts)
    # Identical text collapses to a single entry even across drifted
    # timestamps (persistence replays and lineage merges re-emit the same
    # typed turn with slightly different ts values).
    assert texts == ["corrige le brief"]
    assert [r["ts"] for r in brief["requests"]] == [10.0]


def test_goal_block_included_when_active(tmp_path):
    sess = _make_session(tmp_path)
    goal_state = SimpleNamespace(status="active", goal="livrer la feature contexte", turns_used=3, max_turns=20)
    with patch("api.goals.goal_state_snapshot", return_value=goal_state):
        brief = context_brief.build_deterministic_brief(sess, SID, source="webui")
    assert brief["goal"] == {"text": "livrer la feature contexte", "status": "active", "turns_used": 3, "max_turns": 20}


def test_goal_block_omitted_when_cleared(tmp_path):
    sess = _make_session(tmp_path)
    goal_state = SimpleNamespace(status="cleared", goal="ancien", turns_used=3, max_turns=20)
    with patch("api.goals.goal_state_snapshot", return_value=goal_state):
        brief = context_brief.build_deterministic_brief(sess, SID, source="webui")
    assert brief["goal"] is None


def test_in_flight_marks_active_stream_and_background(tmp_path):
    sess = _make_session(tmp_path, active_stream_id="stream-42")
    with patch("api.background.get_background_tasks", return_value=[
        {"task_id": "t1", "status": "running", "prompt": "analyse du chantier " + "x" * 200},
        {"task_id": "t2", "status": "completed", "prompt": "terminé"},
    ]):
        brief = context_brief.build_deterministic_brief(sess, SID, source="webui")
    in_flight = brief["in_flight"]
    assert in_flight["active"] is True
    assert "active_stream_id" in in_flight["details"]
    # Only the running task is listed, prompt excerpted.
    assert [t["task_id"] for t in in_flight["background_tasks"]] == ["t1"]
    assert len(in_flight["background_tasks"][0]["prompt"]) <= 121


# ── LLM brief persistence + staleness ────────────────────────────────────

def test_llm_brief_persisted_and_flagged_stale(tmp_path):
    sess = _make_session(tmp_path)
    saved = context_brief._save_llm_brief(
        sess, SID, text="## Demandes\n- x\n\n## Accompli\ny\n\n## Reste à faire\nz",
        source="auxiliary-llm", message_count=len(sess.messages),
    )
    assert saved is not None
    store = tmp_path / "webui" / "context-briefs" / f"{SID}.json"
    assert store.exists()

    with _patch_resolution(sess):
        brief = context_brief.get_brief_payload(SID)
    assert brief["llm_brief"]["stale"] is False
    assert brief["llm_brief"]["source"] == "auxiliary-llm"

    # Transcript moved on → stale.
    sess.messages = list(sess.messages) + [{"role": "user", "content": "nouvelle demande", "timestamp": 1004.0}]
    with _patch_resolution(sess):
        brief = context_brief.get_brief_payload(SID)
    assert brief["llm_brief"]["stale"] is True


def test_get_brief_payload_404_for_unknown_session(tmp_path):
    from api import models

    def _missing(sid, **kw):
        raise KeyError(sid)

    with patch.object(models, "get_session", _missing), patch.object(
        models, "get_cli_session_messages", lambda sid, **kw: []
    ):
        with pytest.raises(context_brief.BriefError) as excinfo:
            context_brief.get_brief_payload("unknown-session")
    assert excinfo.value.status == 404


def test_cli_session_falls_back_to_state_db(tmp_path):
    from api import models

    def _missing(sid, **kw):
        raise KeyError(sid)

    cli_messages = [
        {"role": "user", "content": "demande cli", "timestamp": 2000.0},
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 fait en cli", "timestamp": 2001.0},
    ]
    with patch.object(models, "get_session", _missing), patch.object(
        models, "get_cli_session_messages", lambda sid, **kw: cli_messages
    ):
        brief = context_brief.get_brief_payload(SID)
    assert brief["meta"]["source"] == "state_db"
    assert brief["meta"]["message_count"] == 2
    assert brief["requests"][0]["text"] == "demande cli"


def test_legacy_brief_without_transcript_digest_is_always_unverifiable(tmp_path):
    sess = _make_session(tmp_path)
    sess.updated_at = None
    payload = {
        "message_count_at_generation": len(sess.messages),
        "generated_at": time.time(),
        "text": "legacy",
    }

    assert context_brief._llm_brief_is_current(sess, payload, len(sess.messages)) is False
    sess.messages[0] = dict(sess.messages[0], content="same-count rewrite")
    assert context_brief._llm_brief_is_current(sess, payload, len(sess.messages)) is False


# ── job lifecycle ────────────────────────────────────────────────────────

def test_brief_job_generates_fallback_without_aux_model(tmp_path):
    sess = _make_session(tmp_path)
    with _patch_resolution(sess), patch(
        "agent.auxiliary_client.call_llm",
        side_effect=RuntimeError("auxiliary model unavailable in unit test"),
    ):
        job = context_brief.start_brief_job(SID)
        assert job["status"] == "running"
        job_id = job["job_id"]

        deadline = time.time() + 10
        while time.time() < deadline:
            snap = context_brief.brief_job_status(job_id)
            assert snap is not None
            if snap["status"] != "running":
                break
            time.sleep(0.05)
        snap = context_brief.brief_job_status(job_id)
        assert snap is not None
    assert snap["status"] == "done", snap.get("error")
    result = snap["result"]
    # No auxiliary client in the test environment → honest deterministic fallback.
    assert result["brief_source"] == "fallback-template"
    assert result["persisted"] is True
    llm = result["llm_brief"]
    assert "## Demandes" in llm["text"] and "## Accompli" in llm["text"] and "## Reste à faire" in llm["text"]
    assert llm["message_count_at_generation"] == len(sess.messages)

    # The persisted layer is now served by the deterministic payload.
    with _patch_resolution(sess):
        brief = context_brief.get_brief_payload(SID)
    assert brief["llm_brief"] is not None
    assert brief["llm_brief"]["stale"] is False


def test_manual_brief_refresh_persists_for_archived_session(tmp_path):
    sess = _make_session(tmp_path)
    sess.archived = True
    with _patch_resolution(sess), patch.object(
        context_brief,
        "_generate_llm_brief",
        return_value=("x" * 300, "auxiliary-llm"),
    ):
        job = context_brief.start_brief_job(SID)  # manual (automatic= arrives with the auto layer)
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot = context_brief.brief_job_status(job["job_id"])
            assert snapshot is not None
            if snapshot["status"] != "running":
                break
            time.sleep(0.01)
        snapshot = context_brief.brief_job_status(job["job_id"])
        assert snapshot is not None

    assert snapshot["status"] == "done", snapshot.get("error")
    assert snapshot["result"]["persisted"] is True


def test_manual_refresh_does_not_persist_after_successor_admission(tmp_path):
    sess = _make_session(tmp_path)
    saved = []

    def admit_then_return(*_args, **_kwargs):
        sess.active_stream_id = "successor"
        sess.pending_user_message = "next turn"
        return "manual brief", "auxiliary-llm"

    with _patch_resolution(sess), patch.object(
        context_brief,
        "_generate_llm_brief",
        side_effect=admit_then_return,
    ), patch.object(
        context_brief,
        "_save_llm_brief",
        side_effect=lambda *_args, **_kwargs: saved.append(True),
    ):
        job = context_brief.start_brief_job(SID)  # manual (automatic= arrives with the auto layer)
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot = context_brief.brief_job_status(job["job_id"])
            assert snapshot is not None
            if snapshot["status"] != "running":
                break
            time.sleep(0.01)

    assert snapshot["status"] == "error"
    assert "resumed" in snapshot["error"]
    assert saved == []


def test_brief_job_refuses_duplicate_and_empty(tmp_path):
    from api import models

    sess = _make_session(tmp_path)

    # Force the job to stay running so the duplicate check triggers.
    started = __import__("threading").Event()

    def _slow_generate(session, sid, deterministic, **_kwargs):
        started.wait(timeout=5)
        return "x" * 300, "auxiliary-llm"

    with _patch_resolution(sess), patch.object(context_brief, "_generate_llm_brief", _slow_generate):
        job = context_brief.start_brief_job(SID)
        assert job["status"] == "running"
        with pytest.raises(context_brief.BriefError) as excinfo:
            context_brief.start_brief_job(SID)
        assert excinfo.value.status == 409
        started.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot = context_brief.brief_job_status(job["job_id"])
            assert snapshot is not None
            if snapshot["status"] != "running":
                break
            time.sleep(0.01)
        snapshot = context_brief.brief_job_status(job["job_id"])
        assert snapshot is not None
        assert snapshot["status"] == "done"

    empty_sid = "20260812_120000_empty9"
    empty = _make_session(tmp_path, messages=[])
    with patch.object(models, "get_session", lambda sid, **kw: empty if sid == empty_sid else (_ for _ in ()).throw(KeyError(sid))):
        with pytest.raises(context_brief.BriefError) as excinfo:
            context_brief.start_brief_job(empty_sid)
    assert excinfo.value.status == 400


def test_brief_job_status_unknown_returns_none():
    assert context_brief.brief_job_status("no-such-job") is None


def test_delete_stored_brief_removes_cache_and_never_raises(tmp_path):
    store = tmp_path / "context-briefs"
    store.mkdir()
    brief_file = store / f"{SID}.json"
    brief_file.write_text('{"format": 1, "text": "x"}', encoding="utf-8")
    context_brief.delete_stored_brief(tmp_path, SID)
    assert not brief_file.exists()
    # Missing file and missing directory are both silent no-ops.
    context_brief.delete_stored_brief(tmp_path, SID)
    context_brief.delete_stored_brief(tmp_path / "absent-root", SID)


def test_start_job_registers_under_lock_after_resolution(tmp_path, monkeypatch):
    """Regression guard for the duplicate-enqueue race: the 409 check and the
    registration are one critical section, and resolution happens first."""
    sess = _make_session(tmp_path)
    events = []

    real_resolve = context_brief._resolve_session

    def _spy_resolve(sid):
        events.append("resolve")
        return real_resolve(sid)

    monkeypatch.setattr(context_brief, "_resolve_session", _spy_resolve)
    with _patch_resolution(sess), patch.object(
        context_brief, "_generate_llm_brief", return_value=("x" * 300, "auxiliary-llm")
    ):
        job = context_brief.start_brief_job(SID)
        # start_brief_job resolves first; the worker thread re-resolves.
        assert events and events[0] == "resolve"
        deadline = time.time() + 10
        while time.time() < deadline:
            if context_brief.brief_job_status(job["job_id"])["status"] != "running":
                break
            time.sleep(0.05)
        assert context_brief.brief_job_status(job["job_id"])["status"] == "done"
