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
from api.streaming import _assign_stable_message_ids


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
