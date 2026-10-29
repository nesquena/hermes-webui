"""#7862 re-gate (2026-10-27): the six findings from the maintainer's
CHANGES_REQUESTED review at head ``570c878de9d7``.

The review confirmed the ``send()`` ReferenceError fixed, then returned KICK
BACK from both Codex and the senior advisor: three earlier findings still
reproduce and three new paths were found.

Prior findings still open:

1. **Two processes can run the same restored continuation** (CORE) — the
   registry lock covers ONE process and consumption trusts stale memory, so
   two starts both returned 200 with ``goal_related=True``.
2. **A failed start is recorded as executed** (CORE) — a forced thread-start
   failure plus ENOSPC during rollback leaves zero workers, journal
   ``[submitted, interrupted/start_compensated]``, cold restore 0.
3. **Delete reports success while keeping the private prompt** (SILENT) —
   ENOSPC during retirement: delete returns 200 ``ok:true``, the prompt
   stays on disk, cold restore recovers the deleted intent.

New:

4. **Restart then resume loses goal admission** (CORE) — restoration never
   rebuilds ``PENDING_GOAL_CONTINUATION_PROMPTS``, the orphan sweep removes
   the marker, ``continue`` after ``/goal resume`` launches with
   ``goal_related=False``.
5. **Tokenless regeneration consumes the next queued continuation** (CORE) —
   native continuation prompts repeat, so regenerating that text without a
   token spends the intent.
6. **``/goal status`` clears the resume token** (SILENT) — ``/goal resume``
   -> ``/goal status`` -> ``continue`` posts an empty token.

Every test here drives the REAL production shape: the real producer
(``api.goals.register_pending_goal_continuation``), the real store
(``api.goal_continuation_store``), the real route
(``_start_chat_stream_for_session``), the real turn journal, the real delete
route, and the REAL browser token block from ``static/commands.js`` (node,
no browser). RED-first: each test fails on ``570c878de9d7`` for the exact
defect the reviewer described.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = REPO_ROOT / "static" / "commands.js"
UI_JS = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

PROMPT = "keep refining the release checklist"


# ── fixtures / helpers (round-13 shape) ──────────────────────────────────


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (round-12/13 shape)."""
    from api import goal_continuation_store as store
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._RETIRED_LOG.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _cold_reset(store):
    """Simulate a fresh process: drop in-memory state, keep the registry file."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_PROMPTS.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._GENERATION = 0


_MEMORY_ATTRS = (
    "_TOMBSTONES",
    "_RETIRED_GENERATIONS",
    "_CONTINUATION_HANDOFF_TOKENS",
    "_ADOPTED_HANDOFF_SESSIONS",
    "_ROLLBACK_RECEIPTS",
)


def _snapshot_memory(store):
    """Deep-copy this process's registry memory (a 'second process' view)."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    snap = {
        "PENDING_GOAL_CONTINUATION": set(PENDING_GOAL_CONTINUATION),
        "PENDING_GOAL_CONTINUATION_RECORDS": copy.deepcopy(PENDING_GOAL_CONTINUATION_RECORDS),
        "_GENERATION": store._GENERATION,
    }
    for attr in _MEMORY_ATTRS:
        snap[attr] = copy.deepcopy(getattr(store, attr))
    return snap


def _restore_memory(store, snap):
    """Put a previously snapshotted process view back (stale by construction)."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION.update(snap["PENDING_GOAL_CONTINUATION"])
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.update(
            copy.deepcopy(snap["PENDING_GOAL_CONTINUATION_RECORDS"])
        )
        store._GENERATION = snap["_GENERATION"]
        for attr in _MEMORY_ATTRS:
            getattr(store, attr).clear()
            getattr(store, attr).update(copy.deepcopy(snap[attr]))


def _make_session(sid, workspace):
    """Create a REAL persisted session (the route serializes it)."""
    from api.models import SESSIONS, Session

    session = Session(
        session_id=sid,
        title="Goal continuation",
        workspace=str(workspace),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "earlier", "timestamp": 1}],
    )
    session.save()
    SESSIONS[sid] = session
    return session


def _clean_journals(sid):
    from api.models import SESSION_DIR

    for sub in ("_run_journal", "_turn_journal"):
        target = SESSION_DIR / sub / sid
        try:
            for p in target.rglob("*"):
                if p.is_file():
                    p.unlink()
            target.rmdir()
        except (FileNotFoundError, OSError):
            pass


# ── finding 1: cross-process registry transactions ──────────────────────


class TestCrossProcessRegistryTransaction:
    """The registry lock covers ONE process and consumption trusts stale
    memory: two processes can run the same restored continuation. Every
    registry writer must take a cross-process (file) lock, reload disk state
    under it, and validate the token and generation before committing."""

    def test_a_stale_second_process_cannot_spend_what_another_process_spent(
        self, clean_registry
    ):
        store = clean_registry
        from api import goals

        sid = "sess-r14-xproc-consume"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert cont_id and len(cont_id) == 32

        # Process B's own startup: it restores the durable record into ITS
        # memory (the same module, snapshotted as an independent view).
        _cold_reset(store)
        assert store.restore_goal_continuations() == 1
        process_b = _snapshot_memory(store)

        # Process A consumes the intent (its own thread of the same shape):
        # the record leaves memory AND disk in one atomic snapshot.
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-A")
            == store.CONSUME_COMMITTED
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert sid not in records, "the consume did not remove the durable record"

        # Process B still holds its pre-consume copy in memory. Its consume
        # MUST re-read the disk state under the cross-process lock and refuse:
        # the intent was already spent by A.
        _restore_memory(store, process_b)
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-B")
            == store.CONSUME_NOT_MATCHING
        ), (
            "a stale second process re-spent a continuation another process "
            "already consumed: both starts would return 200 goal_related=True "
            "(CORE finding 1)"
        )

    def test_a_writer_snapshot_reloads_peer_arms_under_the_lock(self, clean_registry):
        store = clean_registry
        from api import goals

        sid_x = "sess-r14-xproc-x"
        sid_y = "sess-r14-xproc-y"
        sid_z = "sess-r14-xproc-z"
        goals.register_pending_goal_continuation(sid_x, PROMPT)
        goals.register_pending_goal_continuation(sid_y, PROMPT)

        # Process B's startup view: X and Y live in its memory.
        _cold_reset(store)
        assert store.restore_goal_continuations() == 2
        process_b = _snapshot_memory(store)

        # Process A arms Z durably; B never saw it.
        goals.register_pending_goal_continuation(sid_z, PROMPT)

        # B consumes ITS OWN intent (Y) — a legitimate transaction whose
        # snapshot must first reload the disk state under the cross-process
        # lock, so A's Z survives B's write.
        _restore_memory(store, process_b)
        assert (
            store.consume_pending_goal_continuation(sid_y, PROMPT, "", "att-B")
            == store.CONSUME_COMMITTED
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert sid_x in records, (
            "B's write dropped X, a peer's arm B never saw: the snapshot did "
            "not reload disk state under the cross-process lock (CORE finding 1)"
        )
        assert sid_z in records, (
            "B's write dropped Z, the peer's newest arm: the snapshot did not "
            "reload disk state under the cross-process lock (CORE finding 1)"
        )

    def test_the_registry_lock_serializes_writers_across_processes(self, clean_registry):
        store = clean_registry

        lock_path = store._REGISTRY_LOCK_FILE
        # Another process holds the cross-process registry lock.
        holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_EX)
            done = threading.Event()

            def _writer():
                store.arm_pending_goal_continuation(
                    "sess-r14-xproc-lock", PROMPT, continuation_id="a" * 32
                )
                done.set()

            writer = threading.Thread(target=_writer, daemon=True)
            writer.start()
            try:
                assert not done.wait(0.5), (
                    "a registry writer committed while another process held "
                    "the cross-process lock: there is no file lock (CORE "
                    "finding 1)"
                )
            finally:
                fcntl.flock(holder_fd, fcntl.LOCK_UN)
            assert done.wait(5), "the blocked writer never completed after release"
            writer.join(timeout=5)
        finally:
            os.close(holder_fd)


# ── finding 2: a failed start is not execution ──────────────────────────


class TestFailedStartIsNotExecution:
    """A forced thread-start failure plus ENOSPC during rollback leaves zero
    workers and the journal ``[submitted, interrupted/start_compensated]``.
    Only ``worker_started``, ``completed`` or worker-written journal rows are
    execution; route-generated interruption rows are not."""

    def _arm_and_bind(self, store, sid, stream_id, attempt_id):
        from api import goals

        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert cont_id and len(cont_id) == 32
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, attempt_id)
            == store.CONSUME_COMMITTED
        )
        # The route binds the stream identity BEFORE thr.start(); a forced
        # thread-start failure after this binding is exactly the window.
        assert store.record_goal_continuation_stream(sid, attempt_id, stream_id) is True

    def test_a_route_compensated_start_is_not_execution_evidence(self, clean_registry):
        from api.turn_journal import append_turn_journal_event

        store = clean_registry
        sid = "sess-r14-compensated"
        stream_id = "stream-r14-compensated"
        self._arm_and_bind(store, sid, stream_id, "att-comp")

        # The route's compensation journal: submitted + the route-generated
        # interruption row. The worker never ran.
        append_turn_journal_event(
            sid,
            {
                "event": "submitted",
                "stream_id": stream_id,
                "role": "user",
                "content": PROMPT,
                "created_at": time.time(),
            },
        )
        append_turn_journal_event(
            sid,
            {
                "event": "interrupted",
                "stream_id": stream_id,
                "reason": "start_compensated",
            },
        )

        # The owner's clock is aged past the reclaim window first (round 15,
        # CORE finding 2): a freshly minted handoff is left for its live
        # owner, and this owner crashed — the route compensation proves no
        # worker ran.
        with store._LOCK:
            store._CONTINUATION_HANDOFF_TOKENS[(sid, "att-comp")][
                "_handoff_minted_at"
            ] = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 60
        with store._LOCK:
            from api.config import PENDING_GOAL_CONTINUATION_RECORDS

            store._write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS, context="aging crash owner"
            )

        _cold_reset(store)
        assert store.restore_goal_continuations() == 1, (
            "cold restore dropped a continuation whose worker never ran: the "
            "route-generated interrupted/start_compensated row was counted as "
            "execution (CORE finding 2)"
        )

    def test_a_worker_interrupted_row_is_execution_evidence(self, clean_registry):
        """Control: a WORKER-written interruption (a cancelled running turn)
        IS execution, so the handoff must stay dropped."""
        from api.turn_journal import append_turn_journal_event

        store = clean_registry
        sid = "sess-r14-worker-cancelled"
        stream_id = "stream-r14-cancelled"
        self._arm_and_bind(store, sid, stream_id, "att-cancel")

        append_turn_journal_event(
            sid,
            {
                "event": "worker_started",
                "stream_id": stream_id,
                "created_at": time.time(),
            },
        )
        append_turn_journal_event(
            sid,
            {
                "event": "interrupted",
                "stream_id": stream_id,
                "reason": "cancelled",
                "created_at": time.time(),
            },
        )

        _cold_reset(store)
        assert store.restore_goal_continuations() == 0, (
            "a worker-executed-and-cancelled turn must stay spent; the "
            "handoff was re-adopted for a turn that already ran"
        )


# ── finding 3: delete must not lie over a surviving private prompt ──────


class TestDeleteRetirementHonesty:
    """With ENOSPC during retirement the delete returned 200 ok:true while
    the continuation prompt stayed on disk. Retirement's return value must
    be checked, and deleted-session tombstones must filter BOTH the record
    and the handoff restoration."""

    def test_restore_skips_deleted_session_tombstones(self, clean_registry):
        from api import goals
        from api.models import (
            _load_webui_deleted_session_tombstone,
            _record_webui_deleted_session_tombstone,
        )

        store = clean_registry
        sid = "sess-r14-deleted-tombstone"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        # An in-flight start consumed the intent: the durable handoff is the
        # only evidence left (record gone, handoff on disk).
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-del")
            == store.CONSUME_COMMITTED
        )
        # The session was deleted (its retirement write failed, but the
        # WebUI tombstone landed).
        _record_webui_deleted_session_tombstone(sid)
        try:
            assert sid in _load_webui_deleted_session_tombstone()

            _cold_reset(store)
            assert store.restore_goal_continuations() == 0, (
                "cold restore recovered the intent of a DELETED session from "
                "its surviving handoff: the deleted-session tombstone did not "
                "filter the restoration (SILENT finding 3)"
            )
            _records, _tombstones, handoffs = store._load_file_raw()
            assert not any(k[0] == sid for k in handoffs), (
                "the deleted session's handoff stayed claimable on disk"
            )
        finally:
            from api.models import _clear_webui_deleted_session_tombstone

            _clear_webui_deleted_session_tombstone(sid)

    def test_delete_route_reports_failure_when_retirement_cannot_commit(
        self, tmp_path, monkeypatch
    ):
        import api.models as models
        import api.routes as routes

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
        models.SESSIONS.clear()

        from api import goal_continuation_store as store
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

        sid = "sess-r14-delete-route"
        models.Session(session_id=sid, title="Session").save(
            touch_updated_at=False, skip_index=True
        )
        # A durable pending continuation with its private prompt.
        from api import goals

        goals.register_pending_goal_continuation(sid, PROMPT)

        real_replace = os.replace

        def _enospc_registry_replace(src, dst):
            if "pending_goal_continuations" in str(dst):
                raise OSError("disk full (simulated)")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _enospc_registry_replace)
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
        monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
        monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
        monkeypatch.setattr(
            routes, "_session_is_subagent_view_only", lambda value: False
        )
        captured = {}
        handler = type(
            "H",
            (),
            {
                "command": "POST",
                "wfile": None,
                "send_response": lambda self, status: None,
                "send_header": lambda self, key, value: None,
                "end_headers": lambda self: None,
            },
        )()
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, status=200, extra_headers=None: captured.update(
                payload=payload, status=status
            )
            or True,
        )

        from types import SimpleNamespace

        assert (
            routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
            is True
        )
        payload = captured.get("payload") or {}
        assert payload.get("ok") is not True, (
            "delete reported ok:true while the continuation retirement could "
            "not commit: the private prompt survives on disk and a cold "
            "restore recovers the deleted intent (SILENT finding 3)"
        )
        assert captured.get("status") == 500
        _clean_journals(sid)


# ── finding 4: restart-then-resume must not lose goal admission ─────────


class TestRestoreRebuildsThePromptMirror:
    """Restoration never rebuilt ``PENDING_GOAL_CONTINUATION_PROMPTS``, so
    the orphan sweep removed the marker and ``continue`` after ``/goal
    resume`` launched with ``goal_related=False``. The prompt/token mirror
    must be restored alongside the records, published with the marker under
    their shared lock."""

    def test_a_restored_record_publishes_the_prompt_mirror(self, clean_registry):
        from api import goals
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
        )

        store = clean_registry
        sid = "sess-r14-mirror-record"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        _cold_reset(store)
        assert store.restore_goal_continuations() == 1

        mirror = PENDING_GOAL_CONTINUATION_PROMPTS.get(sid)
        assert isinstance(mirror, dict), (
            "the restore did not rebuild PENDING_GOAL_CONTINUATION_PROMPTS: "
            "the orphan sweep removes the marker and continue after /goal "
            "resume launches with goal_related=False (CORE finding 4)"
        )
        assert mirror.get("prompt") == PROMPT
        assert mirror.get("continuation_id") == cont_id
        # The goals-side orphan sweep must keep the restored marker.
        assert sid in PENDING_GOAL_CONTINUATION
        assert goals.sweep_expired_goal_continuations() == 0, (
            "the orphan sweep removed a restored marker whose mirror was "
            "never published"
        )
        # The legacy key-omission resolver must see the restored token.
        assert goals.peek_pending_goal_continuation_id(sid) == cont_id

    def test_an_adopted_handoff_publishes_the_mirror_too(self, clean_registry):
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_PROMPTS

        store = clean_registry
        sid = "sess-r14-mirror-handoff"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-mir")
            == store.CONSUME_COMMITTED
        )
        # The crashed owner's liveness clock goes stale first (round 15,
        # CORE finding 2) so the cold restore adopts the handoff.
        with store._LOCK:
            store._CONTINUATION_HANDOFF_TOKENS[(sid, "att-mir")][
                "_handoff_minted_at"
            ] = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 60
        with store._LOCK:
            from api.config import PENDING_GOAL_CONTINUATION_RECORDS

            store._write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS, context="aging crash owner"
            )
        _cold_reset(store)
        assert store.restore_goal_continuations() == 1

        mirror = PENDING_GOAL_CONTINUATION_PROMPTS.get(sid)
        assert isinstance(mirror, dict), (
            "an adopted handoff restored the record without the prompt/token "
            "mirror: the resume continue cannot be admitted by identity "
            "(CORE finding 4)"
        )
        assert mirror.get("continuation_id") == cont_id


# ── finding 5: tokenless regeneration must not consume ──────────────────


class TestTokenlessRegenerationStaysOrdinary:
    """Native continuation prompts repeat, so regenerating that text without
    a token consumed the next queued continuation. A non-empty RESOLVED
    ``goal_continuation_id`` is required before consuming; the legacy
    key-omission handling stays in the resolver."""

    def test_a_tokenless_regenerate_of_the_continuation_text_does_not_consume(
        self, clean_registry, monkeypatch, tmp_path
    ):
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r14-regen"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        session = _make_session(sid, tmp_path)

        captured = []
        import api.routes as routes

        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            # The regenerate wire shape: the retained user message IS the
            # continuation prompt, and the body carries NO token — the
            # resolver answers "" for an explicit regenerate.
            response = routes._start_chat_stream_for_session(
                session,
                msg=PROMPT,
                workspace=str(tmp_path),
                model="test-model",
                model_provider="test-provider",
                goal_continuation_id="",
            )
            assert int(response.get("_status", 200) or 200) == 200
            deadline = time.time() + 2.0
            while not captured and time.time() < deadline:
                time.sleep(0.01)
            assert captured, "the worker boundary never ran"
            assert captured[0].get("goal_related") is not True, (
                "a tokenless regeneration of the continuation text was "
                "admitted as the continuation: it consumed the next queued "
                "intent (CORE finding 5)"
            )
            # The queued intent stays pending for its real dispatch.
            records, _tombstones, _handoffs = store._load_file_raw()
            assert records.get(sid, {}).get("continuation_id") == cont_id, (
                "the tokenless regeneration consumed the durable intent"
            )
            assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        finally:
            _clean_journals(sid)

    def test_the_tokenized_continuation_still_consumes(
        self, clean_registry, monkeypatch, tmp_path
    ):
        """Control: the real browser dispatch (token present) keeps working."""
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r14-regen-control"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        session = _make_session(sid, tmp_path)

        captured = []
        import api.routes as routes

        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            response = routes._start_chat_stream_for_session(
                session,
                msg=PROMPT,
                workspace=str(tmp_path),
                model="test-model",
                model_provider="test-provider",
                goal_continuation_id=cont_id,
            )
            assert int(response.get("_status", 200) or 200) == 200
            deadline = time.time() + 2.0
            while not captured and time.time() < deadline:
                time.sleep(0.01)
            assert captured, "the worker boundary never ran"
            assert captured[0].get("goal_related") is True
            assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        finally:
            _clean_journals(sid)


# ── finding 6: /goal status must keep the resume token ──────────────────


_GOAL_TOKEN_DRIVER = r"""
const fs = require('fs');
const commandsSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc = fs.readFileSync(process.argv[3], 'utf8');
const scenario = process.argv[4] || '';
const failures = [];
function check(cond, msg){ if(!cond) failures.push(msg); }

// comment/string/template-aware brace scanner (same extractor as round 13)
function scanEnd(src, openBrace){
  let i = openBrace + 1, depth = 1, n = src.length, state = 'code', quote = '';
  while(i < n && depth > 0){
    const c = src[i], nxt = i + 1 < n ? src[i+1] : '';
    if(state === 'code'){
      if(c === '/' && nxt === '/'){ state = 'linecomment'; i += 2; continue; }
      if(c === '/' && nxt === '*'){ state = 'blockcomment'; i += 2; continue; }
      if(c === '"' || c === "'"){ state = 'string'; quote = c; i += 1; continue; }
      if(c === '`'){ state = 'template'; i += 1; continue; }
      if(c === '{') depth += 1;
      else if(c === '}'){ depth -= 1; if(depth === 0) return i; }
      i += 1;
    } else if(state === 'linecomment'){
      if(c === '\n') state = 'code';
      i += 1;
    } else if(state === 'blockcomment'){
      if(c === '*' && nxt === '/'){ state = 'code'; i += 2; continue; }
      i += 1;
    } else if(state === 'string'){
      if(c === '\\'){ i += 2; continue; }
      if(c === quote) state = 'code';
      i += 1;
    } else if(state === 'template'){
      if(c === '\\'){ i += 2; continue; }
      if(c === '`') state = 'code';
      i += 1;
    }
  }
  throw new Error('unbalanced braces in ' + src.slice(openBrace, openBrace + 40));
}

// The REAL token helpers from static/ui.js.
const _RESUME_GOAL_CONTINUATION_TOKENS = {};
eval(extractSetter());
function extractSetter(){
  const marker = 'function _setResumeGoalContinuationToken(';
  const start = uiSrc.indexOf(marker);
  if(start < 0) throw new Error('missing _setResumeGoalContinuationToken in ui.js');
  return uiSrc.slice(start, scanEnd(uiSrc, uiSrc.indexOf('{', start)) + 1);
}

// The REAL token-decision block from cmdGoal in static/commands.js: the
// ``if(typeof _setResumeGoalContinuationToken==='function'){ ... }`` region.
const blockMarker = "if(typeof _setResumeGoalContinuationToken==='function'){";
const blockStart = commandsSrc.indexOf(blockMarker);
if(blockStart < 0) throw new Error('missing the resume-token block in commands.js');
const blockText = commandsSrc.slice(blockStart, scanEnd(commandsSrc, commandsSrc.indexOf('{', blockStart)) + 1);

function drive(payload){
  const activeSid = 'sid-goal';
  const r = payload;
  // eslint-disable-next-line no-eval
  eval(blockText);
  return _RESUME_GOAL_CONTINUATION_TOKENS['sid-goal'] || '';
}

const TOKEN = 'resume-tok-1';
if(scenario === 'status-keeps'){
  _RESUME_GOAL_CONTINUATION_TOKENS['sid-goal'] = TOKEN;
  const after = drive({action: 'status', ok: true});
  check(after === TOKEN, '/goal status cleared the resume token: got ' + JSON.stringify(after));
} else if(scenario === 'resume-updates'){
  _RESUME_GOAL_CONTINUATION_TOKENS['sid-goal'] = TOKEN;
  const after = drive({action: 'resume', continuation_id: 'fresh-tok-2'});
  check(after === 'fresh-tok-2', '/goal resume did not update the resume token: got ' + JSON.stringify(after));
} else if(scenario === 'clear-drops'){
  _RESUME_GOAL_CONTINUATION_TOKENS['sid-goal'] = TOKEN;
  const after = drive({action: 'clear', ok: true});
  check(after === '', 'a retiring /goal clear left a stale resume token: got ' + JSON.stringify(after));
} else if(scenario === 'pause-keeps'){
  _RESUME_GOAL_CONTINUATION_TOKENS['sid-goal'] = TOKEN;
  const after = drive({action: 'pause', ok: true});
  check(after === TOKEN, '/goal pause cleared the resume token: got ' + JSON.stringify(after));
} else {
  throw new Error('unknown scenario ' + scenario);
}
console.log('RESULT ' + JSON.stringify({ok: failures.length === 0, failures}));
"""

_GOAL_TOKEN_SCENARIOS = ("status-keeps", "resume-updates", "clear-drops", "pause-keeps")


def _run_goal_token_scenario(scenario: str) -> dict:
    import tempfile

    assert NODE, "node not on PATH"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(_GOAL_TOKEN_DRIVER)
        driver_path = fh.name
    try:
        env = os.environ.copy()
        # node SIGABRTs in this environment unless the channel fd is stripped.
        env.pop("NODE_CHANNEL_FD", None)
        proc = subprocess.run(
            [NODE, driver_path, str(COMMANDS_JS), str(UI_JS), scenario],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        out = (proc.stdout or "") + (proc.stderr or "")
        raise AssertionError(f"node driver produced no RESULT line\n{out[:2000]}")
    finally:
        try:
            os.unlink(driver_path)
        except OSError:
            pass


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("scenario", _GOAL_TOKEN_SCENARIOS)
def test_goal_command_token_lifecycle(scenario):
    """``/goal status`` between ``/goal resume`` and ``continue`` posts an
    empty token: the token must survive status responses, update when a
    continuation id is returned, and clear only for retiring actions."""
    result = _run_goal_token_scenario(scenario)
    assert result.get("ok"), (
        f"scenario {scenario!r} failed:\n- " + "\n- ".join(result.get("failures") or [])
    )
