"""F2 (Astra candidate5-v2): orphan completion must bind the artifact to source.

The route's orphan-completion fallback (``_complete_orphaned_resume_publication_commit``,
api/routes.py:28957-29016) decides whether to write the missing commit-complete
proof after re-reading the live source snapshot and comparing it with *this
retry's own* snapshot.  That comparison is a self-comparison: the retry read the
live source microseconds earlier, so ``fresh_row == source_row`` holds by
construction and can never observe that the orphaned artifact no longer
represents the source.  The transcript produced by that re-read
(``_fresh_messages``) is discarded, so nothing binds the artifact's own
transcript to the live source either.

Consequence: a publication that was terminally rejectable (its transcript moved
after it was written) is *completed* by an unrelated retry — commit proof is
manufactured and the guard is retired, admitting a stale publication as a
committed one.

The publication path itself already uses the correct bind
(``list(getattr(published, "messages", []) or []) == messages`` — routes.py:29511
and :29381).  This regression requires the orphan path to use the same bind: a
transcript-inconsistent orphan stays guarded, a transcript-consistent orphan is
still completed.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_session_resume_in_webui import (  # noqa: F401 (fixture import)
    _F3_STRAND,
    _alpha_db,
    _body,
    _isolate_resume_index,
    _make_state_db,
    _reset_recorder,
    resume_env,
    routes_module,
)


def _publish_resume_once(env, sid):
    """Materialise a real canonical through the production Resume route."""
    routes = env["routes"]
    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error() or env["rec"].payload()
    return env["sessions_dir"] / f"{sid}.json"


def _strand_publishing_record(env, sid):
    """Leave a durable ``publishing`` record owned by a dead foreign process."""
    repo_root = Path(__file__).resolve().parents[1]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _F3_STRAND,
            str(repo_root),
            sid,
            str(env["sessions_dir"]),
        ],
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        capture_output=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr


def _move_source_transcript(db_path: Path, sid: str) -> None:
    """Append to the source transcript without touching row/lineage identity."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "UPDATE messages SET content = content || ' [edited after publication]' "
            "WHERE session_id = ?",
            (sid,),
        )
        assert cur.rowcount > 0, "the source transcript was not actually changed"
        conn.commit()
    finally:
        conn.close()


def _orphaned_verified_publication(env, sid):
    """Reproduce the durable crash window: verified canonical, no proof, guard.

    A publisher that dies between writing the explicit verified marker and
    writing the commit-complete proof leaves exactly this durable state: a
    verified, non-denied canonical plus an un-retired ``publishing`` record
    written by a process that is gone.  Removing the proof file (which lives
    beside the sidecar and binds only the artifact signature) and stranding a
    fresh record recreates that state without depending on thread scheduling.
    """
    models = env["models"]
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    sidecar = _publish_resume_once(env, sid)
    assert models.resume_publication_commit_complete(sid)
    models.resume_commit_path(sid).unlink()
    assert not models.resume_publication_commit_complete(sid)
    assert models.resume_publication_state(models.Session.load(sid)) == (
        models.RESUME_PUBLICATION_VERIFIED
    )
    return sidecar


def test_f2_orphan_completion_cannot_manufacture_proof_for_a_stale_artifact(
    resume_env, monkeypatch  # noqa: F811
):
    """F2: a transcript-inconsistent orphan is never completed, and stays guarded."""
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f2-orphan-stale-transcript"
    _isolate_resume_index(monkeypatch, models, env)

    _orphaned_verified_publication(env, sid)
    # The source transcript moves on AFTER the orphaned artifact was written.
    _move_source_transcript(_alpha_db(env), sid)
    _strand_publishing_record(env, sid)

    assert models._resume_ledger_record_present(sid)
    assert models.resume_publication_authority_blocked(sid)
    assert not models._resume_commit_record_present(sid)

    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 409, env["rec"].payload() or env["rec"].error()
    assert not models._resume_commit_record_present(sid), (
        "orphan completion manufactured commit proof for a stale publication"
    )
    assert models._resume_ledger_record_present(sid), (
        "orphan completion retired the guard of a stale publication"
    )
    assert models.resume_publication_authority_blocked(sid)
    models.SESSIONS.clear()
    loaded = models.Session.load(sid)
    assert loaded is None or not models.session_publication_admissible(loaded)


def test_f2_orphan_completion_still_completes_a_transcript_consistent_orphan(
    resume_env, monkeypatch  # noqa: F811
):
    """Control: the transcript bind must not turn into a blanket refusal."""
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f2-orphan-live-transcript"
    _isolate_resume_index(monkeypatch, models, env)

    _orphaned_verified_publication(env, sid)
    _strand_publishing_record(env, sid)

    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 200, env["rec"].error() or env["rec"].payload()
    assert not models._resume_ledger_record_present(sid)
    assert models.resume_publication_commit_complete(sid)
