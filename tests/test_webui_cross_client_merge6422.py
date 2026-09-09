"""
PR #6422 re-gate regressions: monotonic truncate generation, atomic
merge/replace transaction, and durable row lineage for the cross-client
chat-desync merge (#6299).

Every test composes REAL production objects and helpers — ``Session.save``,
``Session._merge_concurrent_appends``, ``truncate_session_at_keep`` (the
/api/session/truncate + /clear path), ``_assign_stable_message_ids`` (the
production message-id minting path) — and asserts the DURABLE reloaded state
via ``Session.load``, not just helper-local values.

The production-composed normal streaming completion regression lives in
``tests/test_webui_state_db_context_reconciliation.py``
(``test_next_webui_turn_context_includes_state_db_external_messages``), which
drives the real ``_run_agent_streaming`` pipeline end-to-end.
"""
import json
import os

import pytest

import api.models as models
from api.models import Session
from api.session_ops import truncate_session_at_keep
from api.streaming import (
    _assign_stable_message_ids,
    _append_and_persist_terminal_error,
    _persist_with_append_intent,
)


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp directory."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    models.SESSIONS.clear()
    yield session_dir, index_file
    models.SESSIONS.clear()


def _durable_contents(session_dir, sid):
    reloaded = Session.load(sid)
    assert reloaded is not None, f"session {sid} did not persist"
    return [m.get("content") for m in reloaded.messages]


class TestMonotonicTruncateGeneration:
    """Length-based truncation inference is gone: only a strictly NEWER
    ``truncate_generation`` can demote a longer in-memory transcript."""

    def test_pre_turn_checkpoint_same_generation_keeps_completed_turn(self, _isolate_session_dir):
        """The re-gate repro at the merge level: the pre-turn checkpoint is
        SHORTER than the completed in-memory turn but carries the SAME
        generation — it must NOT be treated as a truncation.

        Mirrors the real streaming flow: streaming.py:9007 saves the checkpoint
        before the turn; at completion the in-memory transcript is longer and
        the merge must keep it.
        """
        session_dir, index_file = _isolate_session_dir
        sid = "checkpoint_turn"
        base = [
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
        ]
        # Pre-turn checkpoint (what _run_agent_streaming saves before starting).
        checkpoint = Session(session_id=sid, messages=[dict(m) for m in base])
        checkpoint.save()

        # Completed turn: production load path (records the loaded generation),
        # then the longer in-memory transcript, then the completion merge+save.
        completed = Session.load(sid)
        completed.messages = list(base) + [
            {"role": "user", "content": "new webui turn"},
            {"role": "assistant", "content": "ok"},
        ]
        completed._merge_concurrent_appends()
        completed.save()

        assert _durable_contents(session_dir, sid) == [
            "old user", "old assistant", "new webui turn", "ok",
        ]

    def test_truncate_then_append_truncation_wins(self, _isolate_session_dir):
        """Generation-ordered truncate-vs-append, truncate first: client B
        truncates (bumping ``truncate_generation``) before stale client A
        completes its turn; A's merge must adopt the truncated disk state
        instead of resurrecting deleted rows.
        """
        session_dir, index_file = _isolate_session_dir
        sid = "trunc_then_append"
        base = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # Client B truncates via the production path → truncate_generation 1.
        truncate_session_at_keep(s1, 1)
        s1.save()
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

        # Stale client A (loaded at generation 0 with [q1, a1, q2]) appends
        # its completed turn, then merges.
        stale = Session(session_id=sid, messages=[dict(m) for m in base])
        stale.messages.append({"role": "assistant", "content": "a_fresh"})
        stale._merge_concurrent_appends()
        stale.save()

        # Truncation wins: deleted rows must not resurrect; generation stays 1.
        assert _durable_contents(session_dir, sid) == ["q1"]
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

    def test_append_then_truncate_applies_to_merged_transcript(self, _isolate_session_dir):
        """Generation-ordered truncate-vs-append, append first: client A
        merges+saves its completed turn; a later truncate applies to the
        merged transcript and wins.
        """
        session_dir, index_file = _isolate_session_dir
        sid = "append_then_truncate"
        base = [{"role": "user", "content": "start"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # Client A appends + merges + saves (production completion flow).
        a = Session.load(sid)
        a.messages = list(base) + [
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        a._merge_concurrent_appends()
        a.save()
        assert _durable_contents(session_dir, sid) == ["start", "a1", "q2"]

        # Client B truncates to 1 via the production path.
        b = Session.load(sid)
        truncate_session_at_keep(b, 1)
        b.save()

        assert _durable_contents(session_dir, sid) == ["start"]
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1


class TestDurableRowLineage:
    """The uuid lineage minted by _assign_stable_message_ids distinguishes
    rows that collide on the max+1 integer id, even with identical content."""

    def test_two_clients_identical_content_colliding_ids_both_kept(self, _isolate_session_dir):
        """Real two-client ID allocation: both clients load the same base and
        mint the SAME next integer id (3) for DIFFERENT rows with IDENTICAL
        content.  Equal id + equal content is NOT proof of sameness — the
        durable uuid lineage must keep BOTH rows in the durable reload.
        """
        session_dir, index_file = _isolate_session_dir
        sid = "two_client_same_content"
        base = [
            {"role": "user", "content": "hello", "id": 1, "uuid": "u-base-1"},
            {"role": "assistant", "content": "greeting", "id": 2, "uuid": "u-base-2"},
        ]

        # Client 1: loads base, appends one row through the PRODUCTION id
        # minting path (max+1 → id 3) which also mints a durable uuid.
        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()
        c1_row = {"role": "user", "content": "same follow-up", "id": None}
        _assign_stable_message_ids([c1_row], s1.messages)
        s1.messages.append(c1_row)
        s1.save()

        # Client 2: loads the SAME base, appends a DIFFERENT row that ALSO
        # mints id 3 with IDENTICAL content.
        s2 = Session(session_id=sid, messages=[dict(m) for m in base])
        c2_row = {"role": "user", "content": "same follow-up", "id": None}
        _assign_stable_message_ids([c2_row], s2.messages)
        s2.messages.append(c2_row)
        s2._merge_concurrent_appends()
        s2.save()

        contents = _durable_contents(session_dir, sid)
        assert contents == ["hello", "greeting", "same follow-up", "same follow-up"], contents
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        uuids = [m.get("uuid") for m in persisted["messages"]]
        assert len(set(uuids)) == 4, f"durable lineage must be unique per row: {uuids}"
        assert uuids[2] != uuids[3]


class TestAtomicMergeReplaceTransaction:
    """Merge + generation comparison + final validation + replace is one
    per-session cross-process transaction."""

    def test_sidecar_lock_held_through_atomic_replace(self, _isolate_session_dir, monkeypatch):
        """The per-session cross-process lock is held AT os.replace time: while
        save() runs its transaction, a second open of the session's lock file
        must fail to acquire LOCK_EX non-blockingly."""
        import fcntl

        session_dir, index_file = _isolate_session_dir
        sid = "lock_held_at_replace"
        s = Session(session_id=sid, messages=[{"role": "user", "content": "hello"}])
        s.save()

        original_replace = models.os.replace
        lock_path = session_dir / f".{sid}.sidecar.lock"
        observed = {}

        def _replace_checking_lock(src, dst):
            # The per-session sidecar replace is the one inside the locked
            # transaction.  save() also atomically rewrites the shared
            # _index.json AFTER releasing the session lock — that replace must
            # not be mistaken for the sidecar one.
            if dst != Session(session_id=sid).path:
                return original_replace(src, dst)
            # We are inside save()'s locked transaction: a second fd on the
            # same lock file must NOT acquire LOCK_EX non-blockingly.
            try:
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["acquired"] = True
                except BlockingIOError:
                    observed["acquired"] = False
                finally:
                    os.close(fd)
            except OSError as e:
                observed["error"] = str(e)
            return original_replace(src, dst)

        monkeypatch.setattr(models.os, "replace", _replace_checking_lock)

        s2 = Session(session_id=sid, messages=[{"role": "user", "content": "hello"}])
        s2.messages.append({"role": "assistant", "content": "world"})
        s2.save()

        assert observed.get("acquired") is False, (
            f"sidecar lock must be held through os.replace; observed={observed}"
        )

    def test_writer_in_validate_replace_window_is_merged(self, _isolate_session_dir):
        """A writer that lands between the advisory merge and save() (the final
        validate-to-replace window) is detected by the CAS fingerprint check
        and its rows are merged into the DURABLE result."""
        session_dir, index_file = _isolate_session_dir
        sid = "writer_in_window"
        base = [{"role": "user", "content": "start"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()
        s1.messages.append({"role": "user", "content": "client-1-msg"})
        s1.save()

        # Stale client 2: loads base, appends its own row, runs the ADVISORY
        # merge (captures the fingerprint of [start, client-1-msg]).
        s2 = Session(session_id=sid, messages=[dict(m) for m in base])
        s2.messages.append({"role": "user", "content": "client-2-msg"})
        s2._merge_concurrent_appends()

        # Writer lands in the window before s2.save(): a third client appends
        # [start, client-1-msg, intruder-msg].
        intruder = Session(session_id=sid, messages=[
            {"role": "user", "content": "start"},
            {"role": "user", "content": "client-1-msg"},
            {"role": "user", "content": "intruder-msg"},
        ])
        intruder.save()

        # s2.save() must re-validate under the lock, detect the fingerprint
        # mismatch, re-merge, and preserve ALL rows.
        s2.save()

        assert _durable_contents(session_dir, sid) == [
            "start", "client-1-msg", "intruder-msg", "client-2-msg",
        ]


class TestEmptyTailAndEmptyBaseCAS:
    """Re-gate finding #1: the CAS base identity must be recorded even when
    the disk tail is empty or the base is empty, so two writers that BOTH
    preflight before either save cannot have the second save overwrite the
    first's rows."""

    def test_two_writers_both_preflight_before_any_save_both_rows_kept(self, _isolate_session_dir):
        """Both clients load [start], append divergent rows, and run the
        advisory merge while disk is STILL [start] — the zero-tail shape that
        previously skipped fingerprint recording and let the second save
        overwrite the first."""
        session_dir, index_file = _isolate_session_dir
        sid = "preflight_race"
        base = [{"role": "user", "content": "start"}]

        s0 = Session(session_id=sid, messages=[dict(m) for m in base])
        s0.save()

        # Client 1 loads [start], appends, preflights (zero disk tail).
        c1 = Session.load(sid)
        c1.messages.append({"role": "user", "content": "client-1"})
        c1._merge_concurrent_appends()

        # Client 2 loads [start] BEFORE client 1 saves, appends its own row,
        # and preflights against the SAME [start] disk.
        c2 = Session.load(sid)
        c2.messages.append({"role": "user", "content": "client-2"})
        c2._merge_concurrent_appends()

        # Saves serialize; the second must detect the fingerprint change under
        # the lock and merge, not overwrite.
        c1.save()
        c2.save()

        assert _durable_contents(session_dir, sid) == ["start", "client-1", "client-2"]

    def test_two_first_turns_from_empty_base_both_kept(self, _isolate_session_dir):
        """A brand-new chat persisted as an empty sidecar; both clients load
        the EMPTY base (``_loaded_message_count == 0``), append their first
        turn, and preflight before either saves.  The empty base is a VALID
        CAS base — the second save must re-merge and keep both first turns."""
        session_dir, index_file = _isolate_session_dir
        sid = "empty_base_first_turns"

        # New chat persisted with zero messages (production shape).
        s0 = Session(session_id=sid, messages=[])
        s0.save()

        c1 = Session.load(sid)
        assert c1._loaded_message_count == 0
        c1.messages.append({"role": "user", "content": "first-1"})
        c1._merge_concurrent_appends()

        c2 = Session.load(sid)
        assert c2._loaded_message_count == 0
        c2.messages.append({"role": "user", "content": "first-2"})
        c2._merge_concurrent_appends()

        c1.save()
        c2.save()

        assert _durable_contents(session_dir, sid) == ["first-1", "first-2"]

    def test_second_save_after_empty_base_first_turn_keeps_both(self, _isolate_session_dir):
        """Variant where the second writer LOADS the empty base but only
        preflights AFTER the first writer saved (disk [first-1]): our base is
        empty and disk shares no prefix, so the empty-base branch must
        concatenate disk rows then ours instead of bailing and overwriting."""
        session_dir, index_file = _isolate_session_dir
        sid = "empty_base_serialized"

        s0 = Session(session_id=sid, messages=[])
        s0.save()

        c1 = Session.load(sid)
        c1.messages.append({"role": "user", "content": "first-1"})
        c1._merge_concurrent_appends()

        # c2 loads the EMPTY base before c1 saves, but only preflights after
        # c1's write lands.
        c2 = Session.load(sid)
        assert c2._loaded_message_count == 0
        c2.messages.append({"role": "user", "content": "first-2"})

        c1.save()  # disk now [first-1]

        c2._merge_concurrent_appends()
        c2.save()

        assert _durable_contents(session_dir, sid) == ["first-1", "first-2"]


class TestAuthoritativeGenerationBoundaries:
    """Re-gate finding #2: truncate_generation is authoritative across clear,
    context, and failure boundaries."""

    def test_clear_to_empty_newer_generation_beats_stale_stream(self, _isolate_session_dir):
        """A stale stream must NOT resurrect a newer clear-to-empty
        transcript: the generation comparison runs BEFORE the empty-list
        exits."""
        session_dir, index_file = _isolate_session_dir
        sid = "clear_vs_stale"
        base = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]

        s0 = Session(session_id=sid, messages=[dict(m) for m in base])
        s0.save()

        # Stale client loaded the pre-clear transcript and completes a turn.
        stale = Session.load(sid)
        stale.messages = list(base) + [
            {"role": "user", "content": "stale user"},
            {"role": "assistant", "content": "stale reply"},
        ]

        # Another client clears via the production path → gen 1, messages [].
        clear = Session.load(sid)
        truncate_session_at_keep(clear, 0)
        clear.save()
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["messages"] == []
        assert persisted["truncate_generation"] == 1

        # The stale stream merges + saves: the clear must win.
        stale._merge_concurrent_appends()
        stale.save()

        assert _durable_contents(session_dir, sid) == []
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

    def test_newer_nonempty_generation_adopts_display_and_context(self, _isolate_session_dir):
        """When a newer non-empty generation wins the merge, BOTH the display
        transcript (``messages``) and the model-context truncation state
        (``context_messages``) must be adopted — a stale ``context_messages``
        must never survive to be persisted."""
        session_dir, index_file = _isolate_session_dir
        sid = "gen_adopts_context"
        base_msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        base_ctx = [
            {"role": "user", "content": "ctx-q1"},
            {"role": "assistant", "content": "ctx-a1"},
            {"role": "user", "content": "ctx-q2"},
            {"role": "assistant", "content": "ctx-a2"},
        ]

        s0 = Session(
            session_id=sid,
            messages=[dict(m) for m in base_msgs],
            context_messages=[dict(m) for m in base_ctx],
        )
        s0.save()

        # Client B truncates via the production path: display AND context both
        # shrink to 2 rows, generation bumps to 1.
        b = Session.load(sid)
        truncate_session_at_keep(b, 2)
        b.save()
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1
        assert len(persisted["messages"]) == 2
        assert len(persisted["context_messages"]) == 2

        # Stale client A (loaded at generation 0) completes a turn carrying
        # the FULL old display AND old context.
        stale = Session(
            session_id=sid,
            messages=[dict(m) for m in base_msgs],
            context_messages=[dict(m) for m in base_ctx],
        )
        stale.messages.append({"role": "assistant", "content": "stale reply"})
        stale._merge_concurrent_appends()
        stale.save()

        reloaded = Session.load(sid)
        assert [m.get("content") for m in reloaded.messages] == ["q1", "a1"]
        assert [m.get("content") for m in reloaded.context_messages] == ["ctx-q1", "ctx-a1"]
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

    def test_cache_resident_truncate_then_new_turn_keeps_rows(self, _isolate_session_dir):
        """A successful save advances the object's loaded generation, so a
        cache-resident follow-up turn on the SAME object does not misclassify
        its own persisted generation as a newer external truncation."""
        session_dir, index_file = _isolate_session_dir
        sid = "cache_resident_followup"
        base = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]

        s0 = Session(session_id=sid, messages=[dict(m) for m in base])
        s0.save()

        # Cache-resident object: truncates (gen → 1) and saves...
        resident = Session.load(sid)
        truncate_session_at_keep(resident, 1)
        resident.save()
        assert resident._loaded_truncate_generation == 1

        # ...then — WITHOUT reloading — completes a NEW turn on the same
        # object.  Its own generation on disk must not read as a truncation.
        resident.messages.append({"role": "assistant", "content": "fresh reply"})
        resident._merge_concurrent_appends()
        resident.save()

        assert _durable_contents(session_dir, sid) == ["q1", "fresh reply"]
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

    def test_failed_replace_preserves_truncation_pending_for_retry(self, _isolate_session_dir, monkeypatch):
        """A failed atomic replace must preserve the pending truncation
        intent: the retry stamps a generation STRICTLY greater than disk,
        even when another writer advanced disk in between."""
        session_dir, index_file = _isolate_session_dir
        sid = "failed_replace_retry"
        base = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]

        s0 = Session(session_id=sid, messages=[dict(m) for m in base])
        s0.save()

        s = Session.load(sid)
        truncate_session_at_keep(s, 1)
        assert s._truncation_pending is True

        real_replace = models.os.replace
        failed = {"n": 0}

        def _flaky_replace(src, dst):
            if dst == s.path and failed["n"] == 0:
                failed["n"] += 1
                raise OSError("simulated replace failure")
            return real_replace(src, dst)

        monkeypatch.setattr(models.os, "replace", _flaky_replace)

        with pytest.raises(OSError):
            s.save()

        # Truncation intent survived the failed write; nothing committed.
        assert s._truncation_pending is True
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 0

        # Another client truncates again while our write is down (disk gen 1).
        other = Session.load(sid)
        truncate_session_at_keep(other, 1)
        other.save()
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

        # Retry must stamp STRICTLY greater than disk (2), not merely carry
        # forward the stale in-memory value (1).
        s.save()
        assert s._truncation_pending is False
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 2
        assert _durable_contents(session_dir, sid) == ["q1"]


class TestGenerationAwareRecovery:
    """Re-gate finding #3: recovery is generation-aware and runs under the
    same per-session cross-process lock as Session.save()."""

    def _write_sidecar(self, session_dir, sid, messages, truncate_generation=0, suffix=""):
        path = session_dir / f"{sid}.json{suffix}"
        path.write_text(
            json.dumps({
                "session_id": sid,
                "messages": [{"role": "user", "content": m} for m in messages],
                "truncate_generation": truncate_generation,
            }),
            encoding="utf-8",
        )
        return path

    def test_recovery_forbids_backup_with_lower_truncate_generation(self, _isolate_session_dir):
        """A backup that predates an intentional truncation carries a LOWER
        truncate_generation: recovery must NOT restore it, or the monotonic
        generation would move backward and deleted rows would resurrect."""
        from api.session_recovery import inspect_session_recovery_status, recover_session

        session_dir, index_file = _isolate_session_dir
        sid = "gen_aware"
        live = self._write_sidecar(session_dir, sid, ["q1"], truncate_generation=2)
        bak = self._write_sidecar(
            session_dir, sid,
            ["q1", "a1", "q2", "a2"],
            truncate_generation=1,
            suffix=".bak",
        )

        status = inspect_session_recovery_status(live)
        assert status["recommend"] == "no_action"
        assert status["newer_truncate_generation"] is True

        result = recover_session(live)
        assert result["restored"] is False
        persisted = json.loads(live.read_text(encoding="utf-8"))
        assert [m["content"] for m in persisted["messages"]] == ["q1"]
        assert persisted["truncate_generation"] == 2

    def test_startup_recovery_is_generation_aware(self, _isolate_session_dir):
        """The startup scanner routes through the same generation-aware
        decision: a truncated session is left untouched."""
        from api.session_recovery import recover_all_sessions_on_startup

        session_dir, index_file = _isolate_session_dir
        sid = "gen_aware_startup"
        live = self._write_sidecar(session_dir, sid, ["q1"], truncate_generation=2)
        self._write_sidecar(
            session_dir, sid,
            ["q1", "a1", "q2", "a2"],
            truncate_generation=1,
            suffix=".bak",
        )

        result = recover_all_sessions_on_startup(session_dir)
        assert result["restored"] == 0
        persisted = json.loads(live.read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 2

    def test_recovery_still_restores_backup_at_equal_generation(self, _isolate_session_dir):
        """Control: the #1558 data-loss shape (shrink WITHOUT a generation
        bump) is still restored — generation equality means the shrink was
        NOT an intentional truncation."""
        from api.session_recovery import inspect_session_recovery_status, recover_session

        session_dir, index_file = _isolate_session_dir
        sid = "gen_equal_loss"
        live = self._write_sidecar(session_dir, sid, ["q1"], truncate_generation=0)
        bak = self._write_sidecar(
            session_dir, sid,
            ["q1", "a1", "q2", "a2"],
            truncate_generation=0,
            suffix=".bak",
        )

        status = inspect_session_recovery_status(live)
        assert status["recommend"] == "restore"

        result = recover_session(live)
        assert result["restored"] is True
        persisted = json.loads(live.read_text(encoding="utf-8"))
        assert [m["content"] for m in persisted["messages"]] == ["q1", "a1", "q2", "a2"]
        assert bak.exists()  # the backup source is read-only, never consumed

    def test_recover_session_holds_sidecar_lock_through_replace(self, _isolate_session_dir, monkeypatch):
        """The per-session cross-process lock is held at the recovery replace:
        a second open of the session's lock file must fail to acquire LOCK_EX
        non-blockingly."""
        import fcntl

        from api.session_recovery import recover_session

        session_dir, index_file = _isolate_session_dir
        sid = "lock_held_recover"
        live = self._write_sidecar(session_dir, sid, ["q1"])
        self._write_sidecar(session_dir, sid, ["q1", "q2"], suffix=".bak")
        lock_path = session_dir / f".{sid}.sidecar.lock"
        observed = {}
        real_replace = models.os.replace

        def _replace_checking_lock(src, dst):
            if dst != live:
                return real_replace(src, dst)
            try:
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["acquired"] = True
                except BlockingIOError:
                    observed["acquired"] = False
                finally:
                    os.close(fd)
            except OSError as e:
                observed["error"] = str(e)
            return real_replace(src, dst)

        monkeypatch.setattr(models.os, "replace", _replace_checking_lock)

        result = recover_session(live)
        assert result["restored"] is True
        assert observed.get("acquired") is False, (
            f"sidecar lock must be held through the recovery replace; observed={observed}"
        )


class TestReGate20260824:
    """Re-gate 2026-08-24: release-blocker smoke + production-composed
    regressions for the append-intent, CAS-fail-closed, directory-fsync, and
    integer-ID/no-UUID findings."""

    def test_ordinary_save_durable_reload_smoke(self, _isolate_session_dir):
        """RELEASE BLOCKER repro: a plain `Session.save()` must persist and
        reload.  Previously `payload` was only assigned inside the CAS-rebuild
        arm, so the normal path hit `f.write(payload)` with an unbound
        variable (UnboundLocalError) — every ordinary session save crashed."""
        session_dir, index_file = _isolate_session_dir
        sid = "plain_save_smoke"
        s = Session(
            session_id=sid,
            title="smoke title",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
            context_messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        )
        s.save()  # must NOT raise

        reloaded = Session.load(sid)
        assert reloaded is not None
        assert [m["content"] for m in reloaded.messages] == ["hello", "hi there"]
        assert [m["content"] for m in reloaded.context_messages] == ["hello", "hi there"]
        assert reloaded.title == "smoke title"
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["message_count"] == 2

    def test_gateway_append_intent_preserves_concurrent_rows(self, _isolate_session_dir):
        """Gateway success/error settlement is an APPEND producer: after a
        concurrent writer persisted rows, the gateway's save must reconcile
        (merge) instead of overwriting them."""
        session_dir, index_file = _isolate_session_dir
        sid = "gateway_vs_stream"
        base = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]

        # Streaming client completes first.
        streamer = Session(session_id=sid, messages=[dict(m) for m in base])
        streamer.save()

        # Gateway error settlement: loaded from the same base, appends its
        # error row, and (per the fix) establishes append intent.
        gateway = Session.load(sid)
        gateway.messages.append({"role": "assistant", "content": "gateway error row"})
        gateway._merge_concurrent_appends()
        gateway.save()

        assert _durable_contents(session_dir, sid) == [
            "q1", "a1", "gateway error row",
        ]

    def test_synchronous_append_intent_preserves_concurrent_rows(self, _isolate_session_dir):
        """Synchronous completion producer: a stale client that loaded before
        a concurrent append must keep the disk rows when it saves with append
        intent."""
        session_dir, index_file = _isolate_session_dir
        sid = "sync_vs_stream"
        base = [{"role": "user", "content": "q1"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # Sync client loaded from the base, appends its turn.
        sync_client = Session(session_id=sid, messages=[dict(m) for m in base])
        sync_client.messages.append({"role": "assistant", "content": "sync answer"})
        # Concurrent stream appends a second row before the sync client saves.
        other = Session.load(sid)
        other.messages.append({"role": "assistant", "content": "stream row"})
        other._merge_concurrent_appends()
        other.save()

        sync_client._merge_concurrent_appends()
        sync_client.save()

        assert _durable_contents(session_dir, sid) == [
            "q1", "stream row", "sync answer",
        ]

    def test_cas_exhaustion_fails_closed(self, _isolate_session_dir, monkeypatch):
        """CAS retry exhaustion must ABORT (fail closed), not publish a
        best-effort merge that could overwrite a concurrent append."""
        session_dir, index_file = _isolate_session_dir
        sid = "cas_exhaust"
        s = Session(session_id=sid, messages=[{"role": "user", "content": "q1"}])
        s.save()

        stale = Session.load(sid)
        stale.messages.append({"role": "assistant", "content": "a_fresh"})

        def _never_converges(self):
            # Every re-merge records a fingerprint that never matches the
            # on-disk state → the CAS loop exhausts its 3 attempts.
            self._merge_snapshot_fingerprint = "stale-sentinel-forever"

        monkeypatch.setattr(
            Session, "_merge_concurrent_appends_locked", _never_converges
        )
        stale._merge_concurrent_appends()
        with pytest.raises(RuntimeError, match="CAS retries exhausted"):
            stale.save()

        # Disk untouched: the concurrent append was not clobbered.
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert [m["content"] for m in persisted["messages"]] == ["q1"]

    def test_directory_fsync_failure_is_best_effort(self, _isolate_session_dir, monkeypatch):
        """The parent-directory fsync boundary is best-effort: a platform that
        refuses directory fsync must not fail the save, and the payload must
        still be durable."""
        session_dir, index_file = _isolate_session_dir
        sid = "dir_fsync_fail"
        s = Session(session_id=sid, messages=[{"role": "user", "content": "q1"}])

        real_fsync = models.os.fsync
        calls = {"n": 0}

        def _fsync_then_fail(fd):
            calls["n"] += 1
            real_fsync(fd)
            if calls["n"] == 2:  # second fsync = the directory fd
                raise OSError("directory fsync not supported")

        monkeypatch.setattr(models.os, "fsync", _fsync_then_fail)
        s.save()  # must not raise

        assert _durable_contents(session_dir, sid) == ["q1"]

    def test_integer_id_no_uuid_mints_durable_lineage(self):
        """A genuinely new row whose integer id was supplied by the provider
        must still receive a uuid: two writers minting the SAME integer id for
        different rows are disambiguated by the uuid lineage."""
        rows_a = [{"id": 7, "role": "assistant", "content": "row from A"}]
        rows_b = [{"id": 7, "role": "assistant", "content": "row from B"}]
        stamped_a = _assign_stable_message_ids(rows_a)
        stamped_b = _assign_stable_message_ids(rows_b)

        # The return contract counts newly-assigned INTEGER ids only: both
        # rows already carried id=7, so no integer id was minted (0) — even
        # though the durable uuid lineage WAS minted below (re-gate
        # 2026-08-25: uuid minting is a side effect, not part of the count).
        assert stamped_a == 0 and stamped_b == 0
        assert rows_a[0]["uuid"] and rows_b[0]["uuid"]
        assert rows_a[0]["uuid"] != rows_b[0]["uuid"]
        assert rows_a[0]["id"] == 7 and rows_b[0]["id"] == 7

        # Existing uuid lineage is carried forward untouched.
        row = [{"id": 3, "uuid": "abc123", "role": "assistant"}]
        assert _assign_stable_message_ids(row) == 0
        assert row[0]["uuid"] == "abc123"


class TestReGate20260825:
    """Re-gate 2026-08-25 (maintainer CHANGES_REQUESTED on 7747479ca9ad):
    production-composed coverage for the four terminal append producers that
    still saved without ``_merge_concurrent_appends()``, plus the
    append-intent-consumption regression for a reused cached Session.

    Unlike the earlier gateway/synchronous tests (which manually called
    ``_merge_concurrent_appends()`` on a Session), these execute the REAL
    production callers: ``_append_and_persist_terminal_error`` (the
    result-classified error + outer-settlement path),
    ``_persist_with_append_intent`` (the synchronous self-heal success save),
    and ``cancel_stream()`` (the direct cancel writeback).
    """

    def test_terminal_error_settlement_preserves_concurrent_rows(self, _isolate_session_dir):
        """Result-classified / outer-exception settlement
        (``_append_and_persist_terminal_error``) is an APPEND producer: a row
        a concurrent writer persisted since this object loaded must survive
        the terminal error save."""
        session_dir, index_file = _isolate_session_dir
        sid = "term_error_merge"
        base = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # Stale settlement object loaded BEFORE the concurrent append.
        stale = Session.load(sid)
        concurrent = Session.load(sid)
        concurrent.messages.append({"role": "assistant", "content": "other client row"})
        concurrent._merge_concurrent_appends()
        concurrent.save()

        _append_and_persist_terminal_error(
            stale, {"role": "assistant", "content": "terminal error", "_error": True}
        )

        assert _durable_contents(session_dir, sid) == [
            "q1", "a1", "other client row", "terminal error",
        ]

    def test_synchronous_self_heal_writeback_preserves_concurrent_rows(self, _isolate_session_dir):
        """The synchronous credential self-heal success save
        (``_persist_with_append_intent``) is an APPEND producer: it settles
        the turn's result rows into the transcript and must not clobber a
        concurrent writer's rows."""
        session_dir, index_file = _isolate_session_dir
        sid = "self_heal_merge"
        base = [{"role": "user", "content": "q1"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        stale = Session.load(sid)
        stale.messages.append({"role": "assistant", "content": "healed answer"})

        other = Session.load(sid)
        other.messages.append({"role": "assistant", "content": "stream row"})
        other._merge_concurrent_appends()
        other.save()

        _persist_with_append_intent(stale)

        assert _durable_contents(session_dir, sid) == [
            "q1", "stream row", "healed answer",
        ]

    def test_cancel_stream_writeback_preserves_concurrent_rows(self, _isolate_session_dir):
        """The direct ``cancel_stream()`` partial/cancel writeback executes
        the REAL production cancel path (not a manual helper sequence) and
        must reconcile (merge) concurrent rows instead of overwriting them."""
        import queue
        import threading
        from unittest.mock import patch

        from api import config as live_config
        from api.config import (
            CANCEL_FLAGS,
            STREAMS,
            register_stream_owner,
            unregister_stream_owner,
        )
        from api.streaming import cancel_stream

        session_dir, index_file = _isolate_session_dir
        sid = "cancel_merge"
        stream_id = "stream_cancel_merge"
        base = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # The session object the cancel path will write through — loaded
        # BEFORE the concurrent append (the lost-update shape).
        cached = Session.load(sid)
        cached.active_stream_id = stream_id

        other = Session.load(sid)
        other.messages.append({"role": "assistant", "content": "concurrent row"})
        other._merge_concurrent_appends()
        other.save()

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        register_stream_owner(stream_id, sid)
        try:
            with patch("api.streaming.get_session", return_value=cached):
                assert cancel_stream(stream_id) is True
        finally:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            unregister_stream_owner(stream_id)
            live_config.ACTIVE_RUNS.pop(stream_id, None)

        durable = _durable_contents(session_dir, sid)
        # The concurrent append survived the cancel writeback...
        assert "concurrent row" in durable
        # ...and the cancel marker was persisted on top of the merged tail.
        reloaded = Session.load(sid)
        assert any(
            isinstance(m, dict) and "Task cancelled" in str(m.get("content") or "")
            for m in reloaded.messages
        )

    def test_append_intent_consumed_after_save_truncation_same_object(self, _isolate_session_dir):
        """Durable-reload regression (maintainer finding #3): append intent
        must be CONSUMED by the successful append save.  Reusing ONE cached
        Session — establish append intent and save, then truncate that same
        object and save again — must NOT resurrect the deleted suffix via a
        stale CAS reconciliation against the longer pre-truncation disk
        transcript."""
        session_dir, index_file = _isolate_session_dir
        sid = "stale_append_intent"
        base = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]

        s1 = Session(session_id=sid, messages=[dict(m) for m in base])
        s1.save()

        # ONE cached object reused across both transactions (the flagged shape).
        cached = Session.load(sid)
        cached.messages.append({"role": "assistant", "content": "a2"})
        cached._merge_concurrent_appends()
        cached.save()
        assert _durable_contents(session_dir, sid) == ["q1", "a1", "a2"]

        # Truncate the SAME object via the production entry point.
        truncate_session_at_keep(cached, 1)
        cached.save()

        # The removed suffix stays deleted — no stale append reconciliation.
        assert _durable_contents(session_dir, sid) == ["q1"]
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert persisted["truncate_generation"] == 1

