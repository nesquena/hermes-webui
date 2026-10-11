"""Round-14 re-gate of the merge-pushed head (maintainer review 5481233439,
2026-10-10T23:49:54Z, anchored ``b2f9924a``).

Two findings, both about a stale object being acted on after the thing it
described had already moved on:

1. [CORE] ``api/routes.py:903`` — "project deletion overwrites newer replies
   with stale cached history". The delete's write-through upgraded only
   metadata-only stubs, so a FULL but STALE cached session skipped the
   freshness check entirely and ``save()`` wrote the short in-memory history
   back over a sidecar that was already ahead of it. Reproduced by the
   maintainer over real HTTP with one message cached and two persisted on
   disk: the delete returned success and the next GET showed only one
   message. Fixed by resolving each id through ``get_session(sid)`` (the
   canonical freshness path, which reloads a lagging entry) instead of
   reading the LRU directly, and by re-checking project ownership and
   streaming status on the REFRESHED object before clearing and saving.

2. [SILENT] ``api/routes.py:1259`` — "the new stub upgrade can file a chat
   into the wrong project". ``b2f9924a`` upgrades a resident stub before the
   sweep claims it, but the claim still used the workspace read from the
   stub/index BEFORE the upgrade, so a stub naming bound workspace A whose
   full sidecar says unbound workspace B filed B's chat into A's project.
   Fixed by recomputing the workspace from the refreshed session and
   repeating the profile and view-only eligibility checks before the claim.

Coverage here is deliberately split: the two behavioural tests drive the real
functions over real ``Session`` objects on a temporary session store (so the
freshness resolution, the stub upgrade and the claim all run for real), and
the source-order guards pin the shape of the shipped code so a later edit
cannot quietly restore either hole.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# A parsed ``cached = get_session(sid)`` — whitespace tolerant, since the 6775
# P2 lesson is that a shape guard written as a bare substring stays green for
# every reformatting of the same code.
_GET_SESSION_CALL = re.compile(r"cached\s*=\s*get_session\(\s*sid\s*\)")
_SESSIONS_GET = re.compile(r"SESSIONS\s*\.\s*get\(\s*sid\s*\)")
_S_WS_FROM_SESSION = re.compile(r"s_ws\s*=\s*getattr\(\s*s\s*,\s*[\"']workspace[\"']")
_UPGRADE_CALL = re.compile(r"s\s*=\s*_ensure_full_session_before_mutation\(\s*sid\s*,\s*s\)")
_CLAIM_CALL = re.compile(r"_auto_assign_claim_session\(\s*pid\s*,\s*s\s*,\s*s_ws\s*\)")
_VIEW_ONLY_ON_S = re.compile(r"_auto_assign_target_is_view_only\(\s*s\s*,\s*sid\s*\)")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _function_source(name: str, *, end_marker: str) -> str:
    """Slice one module-level function out of ``api/routes.py``."""
    src = _read("api/routes.py")
    start = src.index(f"def {name}(")
    end = src.index(end_marker, start)
    return src[start:end]


@pytest.fixture
def _isolated_store(tmp_path, monkeypatch):
    """A private session store plus the module names the code paths read.

    ``api.routes`` binds ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` /
    ``SESSIONS`` / ``LOCK`` / ``get_session`` at import time, while
    ``Session.save()`` writes through ``api.models.SESSION_DIR``: both name the
    same temporary directory here so the real load/save round trip runs against
    it. ``SESSIONS`` is cleared, never rebound, because ``api.config``,
    ``api.models`` and ``api.routes`` all hold the same dict object.
    """
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    config.SESSIONS.clear()
    try:
        yield session_dir, index_file, routes, models, config
    finally:
        config.SESSIONS.clear()


def _messages(n: int, tag: str = "m"):
    """``n`` alternating user/assistant messages."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"{tag}-{i}"})
    return out


def _sidecar(session_dir: Path, sid: str) -> dict:
    return json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1 — [CORE] the delete write-through must not save a stale history back
# ---------------------------------------------------------------------------


def test_delete_write_through_refreshes_a_stale_cache_entry(_isolated_store):
    """One cached message must not overwrite the two that are on disk.

    The exact maintainer reproduction: a reply landed AFTER the cache entry was
    populated, so the cache is a full session that merely lags. Pre-fix the
    write-through read the LRU object, ``_ensure_full_session_before_mutation``
    was a no-op for it, and ``save()`` truncated the sidecar to one message.
    """
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round14_stale"
    sid = "sess-round14-stale"

    # Disk truth: two exchanges, still filed under the project.
    disk = models.Session(
        session_id=sid,
        workspace="/ws/round14",
        messages=_messages(2, "disk"),
        project_id=pid,
    )
    disk.save()
    assert _sidecar(session_dir, sid)["message_count"] == 2

    # The cache: a full session for the same id that only knows the first one.
    stale = models.Session(
        session_id=sid,
        workspace="/ws/round14",
        messages=_messages(1, "cache"),
        project_id=pid,
    )
    config.SESSIONS[sid] = stale

    assert routes._persist_cleared_project_ids(pid, [sid]) == 1

    payload = _sidecar(session_dir, sid)
    assert payload["project_id"] is None, "the unlink must still land"
    assert payload["message_count"] == 2, (
        "the newer persisted reply was dropped: the write-through saved the "
        "stale cache entry instead of the refreshed session"
    )

    # The refreshed object is what the cache now holds, with the clear applied.
    assert config.SESSIONS[sid].project_id is None
    assert len(config.SESSIONS[sid].messages) == 2


def test_delete_write_through_upgrades_a_metadata_stub_without_losing_history(
    _isolated_store,
):
    """Control: the #1558 stub path keeps working (history intact, id cleared)."""
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round14_stub"
    sid = "sess-round14-stub"

    full = models.Session(
        session_id=sid,
        workspace="/ws/round14",
        messages=_messages(4, "stub"),
        project_id=pid,
    )
    full.save()

    stub = models.Session.load_metadata_only(sid)
    assert stub is not None and stub._loaded_metadata_only
    config.SESSIONS[sid] = stub

    assert routes._persist_cleared_project_ids(pid, [sid]) == 1

    payload = _sidecar(session_dir, sid)
    assert payload["project_id"] is None
    assert payload["message_count"] == 4


def test_delete_write_through_leaves_a_streaming_session_to_its_worker(
    _isolated_store, monkeypatch
):
    """Control: an actively streaming session is skipped, disk untouched."""
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round14_stream"
    sid = "sess-round14-stream"

    live = models.Session(
        session_id=sid,
        workspace="/ws/round14",
        messages=_messages(2, "stream"),
        project_id=pid,
        active_stream_id="stream-14",
    )
    live.save()
    config.SESSIONS[sid] = live
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"stream-14"})

    assert routes._persist_cleared_project_ids(pid, [sid]) == 0
    assert _sidecar(session_dir, sid)["project_id"] == pid


# ---------------------------------------------------------------------------
# 2 — [SILENT] the sweep must not claim with the pre-upgrade workspace
# ---------------------------------------------------------------------------


def test_sweep_refuses_a_claim_the_refreshed_workspace_does_not_cover(
    _isolated_store, monkeypatch, tmp_path
):
    """Stub says bound workspace A, the full sidecar says unbound B.

    Pre-fix the sweep claimed with the stub's stale ``s_ws`` (A) and filed B's
    chat into A's project. Post-fix the workspace is re-derived from the
    refreshed session and the claim is skipped.
    """
    session_dir, index_file, routes, models, config = _isolated_store
    pid = "proj_round14_sweep"
    sid = "sess-round14-sweep"
    # Real directories: Session normalizes a workspace to its absolute form, so
    # the index row, the project's bound list and the sidecar must all use the
    # same spelling for the loop's `str(ws) in bound` gate to mean anything.
    ws_a = tmp_path / "round14-a"
    ws_b = tmp_path / "round14-b"
    ws_a.mkdir()
    ws_b.mkdir()
    ws_a_s, ws_b_s = str(ws_a), str(ws_b)

    # Disk truth: an EMPTY chat in the UNBOUND workspace B.
    disk = models.Session(session_id=sid, workspace=ws_b_s, messages=[])
    disk.save()
    assert _sidecar(session_dir, sid)["workspace"] == ws_b_s

    # A resident metadata-only stub whose workspace attribute names A (the same
    # divergence the maintainer described: the stub is what the cache holds).
    stub = models.Session.load_metadata_only(sid)
    assert stub is not None and getattr(stub, "_loaded_metadata_only", False)
    stub.workspace = ws_a_s
    stub.project_id = None
    config.SESSIONS[sid] = stub

    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": sid,
                    "workspace": ws_a_s,
                    "profile": "default",
                    "project_id": None,
                    "active_stream_id": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    proj = {
        "project_id": pid,
        "profile": "default",
        "workspaces": [ws_a_s],
        "auto_assign": True,
    }
    monkeypatch.setattr(routes, "load_projects", lambda: [dict(proj)])
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda sid: "")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)

    changed = routes._auto_assign_sweep_body(proj)

    assert changed == 0, (
        "the sweep filed the chat into A's project using the stale stub "
        "workspace instead of the refreshed one (B, unbound)"
    )
    assert _sidecar(session_dir, sid)["project_id"] is None


def test_sweep_still_files_when_the_refreshed_workspace_is_bound(
    _isolated_store, monkeypatch, tmp_path
):
    """Control: the new post-upgrade checks must not over-block a real match."""
    session_dir, index_file, routes, models, config = _isolated_store
    pid = "proj_round14_ok"
    sid = "sess-round14-ok"
    ws_a = tmp_path / "round14-ok"
    ws_a.mkdir()
    ws_a_s = str(ws_a)

    disk = models.Session(session_id=sid, workspace=ws_a_s, messages=[])
    disk.save()

    stub = models.Session.load_metadata_only(sid)
    assert stub is not None and getattr(stub, "_loaded_metadata_only", False)
    config.SESSIONS[sid] = stub

    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": sid,
                    "workspace": ws_a_s,
                    "profile": "default",
                    "project_id": None,
                    "active_stream_id": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    proj = {
        "project_id": pid,
        "profile": "default",
        "workspaces": [ws_a_s],
        "auto_assign": True,
    }
    monkeypatch.setattr(routes, "load_projects", lambda: [dict(proj)])
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda sid: "")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)

    assert routes._auto_assign_sweep_body(proj) == 1
    assert _sidecar(session_dir, sid)["project_id"] == pid


# ---------------------------------------------------------------------------
# 3 — source-order guards: the shipped shape, not a copy of it
# ---------------------------------------------------------------------------


def test_write_through_resolves_through_get_session():
    """``_persist_cleared_project_ids`` must not read the LRU directly."""
    body = _function_source(
        "_persist_cleared_project_ids", end_marker="\ndef _auto_assign_target_is_view_only("
    )
    assert _GET_SESSION_CALL.search(body), (
        "the write-through must resolve the session through get_session(sid) so "
        "a lagging full cache entry is reloaded before it is saved back"
    )
    assert not _SESSIONS_GET.search(body), (
        "the write-through still reads SESSIONS.get(sid) as its session source; "
        "that is the stale-object hole the maintainer reproduced"
    )
    # The two answers the caller's clear acted on are re-checked after the
    # refresh, and only then is the id cleared and published.
    get_i = _GET_SESSION_CALL.search(body).start()
    stream_i = body.index("active_stream_id", get_i)
    proj_i = body.index('getattr(cached, "project_id", None)', get_i)
    clear_i = body.index("cached.project_id = None", get_i)
    assert get_i < proj_i < clear_i, (get_i, proj_i, clear_i)
    assert get_i < stream_i < clear_i, (get_i, stream_i, clear_i)
    # The ownership re-check happens BEFORE the clear, so a row re-filed under
    # another project is never un-filed by the delete (round-8 regression).
    assert proj_i < clear_i


def test_sweep_rederives_the_workspace_after_the_stub_upgrade():
    """The claim must use the workspace of the REFRESHED session."""
    body = _function_source(
        "_auto_assign_sweep_body", end_marker="\ndef _auto_assign_candidate_count("
    )
    upgrade_i = _UPGRADE_CALL.search(body)
    assert upgrade_i is not None, "the stub upgrade is missing from the sweep"
    claim_i = _CLAIM_CALL.search(body)
    assert claim_i is not None, "the sweep no longer claims through the helper"

    after = body[upgrade_i.end():claim_i.start()]
    assert _S_WS_FROM_SESSION.search(after), (
        "after the upgrade the sweep must re-derive s_ws from the refreshed "
        "session; the pre-upgrade workspace filed B's chat into A's project"
    )
    assert _VIEW_ONLY_ON_S.search(after), (
        "after the upgrade the sweep must repeat the view-only eligibility "
        "check on the refreshed session"
    )
    # And the profile check too, between the upgrade and the claim.
    assert "profile" in after, "the post-upgrade profile re-check is missing"
