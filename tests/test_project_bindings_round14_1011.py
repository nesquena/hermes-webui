"""Round-14/15 re-gates of the delete write-through (maintainer reviews
5481233439 @ 2026-10-10T23:49:54Z and 5481568957 @ 2026-10-11T02:08:20Z).

Round 14 — [CORE] ``api/routes.py:903``: "project deletion overwrites newer
replies with stale cached history". The write-through upgraded only
metadata-only stubs, so a FULL but STALE cached session skipped the freshness
check entirely and ``save()`` wrote the short in-memory history back over a
sidecar that was already ahead of it (reproduced over real HTTP: one message
cached, two persisted on disk, and the delete left only one message).

Round 15 — [CORE] ``api/routes.py:907``: the ``get_session`` resolution that
fixed round 14 still trusts a FULL cache entry whose sidecar holds the SAME
number of messages, because ``_cached_session_lags_disk`` compares message
COUNTS. Reproduced over real HTTP: a stale two-message cache entry replaced the
two newer persisted messages (the newer draft was lost) and cleared the project
on a chat that had already been reassigned to project B. Both live in
``_persist_cleared_project_ids`` / ``_delete_target_session``.

The behavioural tests drive the real functions over real ``Session`` objects on
a temporary session store, so the sidecar resolution, the ownership re-check and
the streaming deferral all run for real, and source guards pin the shape of the
shipped write-through (a reformatting must not restore the hole).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# A parsed call to the sidecar resolver — whitespace tolerant, since the 6775 P2
# lesson is that a shape guard written as a bare substring stays green for every
# reformatting of the same code.
_RESOLVER_CALL = re.compile(r"cached\s*=\s*_delete_target_session\([^)]*\)")
_STALE_SOURCE = re.compile(
    r"cached\s*=\s*(SESSIONS\s*\.\s*get\(|get_session\()"
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """Slice one module-level function out of ``api/routes.py``."""
    src = _read("api/routes.py")
    start = src.index(f"def {name}(")
    end = src.index("\n\ndef ", start + 1)
    return src[start:end]


@pytest.fixture
def _isolated_store(tmp_path, monkeypatch):
    """A private session store plus the module names the code paths read.

    ``api.routes`` binds ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` /
    ``SESSIONS`` / ``LOCK`` at import time, while ``Session.save()`` writes
    through ``api.models.SESSION_DIR``: both name the same temporary directory
    here so the real load/save round trip runs against it. ``SESSIONS`` is
    cleared, never rebound, because ``api.config``, ``api.models`` and
    ``api.routes`` all hold the same dict object.
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


def _delete_unlink(routes, pid, sid):
    """The delete's real two-step flow: clear in cache, then write through."""
    cleared_ids: list = []
    routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids)
    return routes._persist_cleared_project_ids(pid, cleared_ids)


# ---------------------------------------------------------------------------
# [CORE] the delete write-through must not save a stale history back
# ---------------------------------------------------------------------------


def test_delete_write_through_refreshes_a_stale_cache_entry(_isolated_store):
    """One cached message must not overwrite the two that are on disk.

    The round-14 maintainer reproduction: a reply landed AFTER the cache entry
    was populated, so the cache is a full session that merely lags. Pre-fix the
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

    assert _delete_unlink(routes, pid, sid) == 1

    payload = _sidecar(session_dir, sid)
    assert payload["project_id"] is None, "the unlink must still land"
    assert payload["message_count"] == 2, (
        "the newer persisted reply was dropped: the write-through saved the "
        "stale cache entry instead of the refreshed session"
    )

    # The refreshed object is what the cache now holds, with the clear applied.
    assert config.SESSIONS[sid].project_id is None
    assert len(config.SESSIONS[sid].messages) == 2


def test_delete_write_through_keeps_a_newer_draft_and_a_reassigned_row(
    _isolated_store,
):
    """[CORE] Equal message counts must not hide a newer sidecar.

    The round-15 reproduction: the stale cache entry and the sidecar carry the
    SAME number of messages, but the sidecar is otherwise newer — it holds the
    user's newer draft and the chat has already been re-filed under project B.
    ``_cached_session_lags_disk`` compares counts, so a ``get_session``-based
    resolution still served the stale copy: its save cleared the project the
    chat had just moved to and dropped the newer draft. Pre-fix this asserted
    the loss; post-fix the write-through skips the row entirely.
    """
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round15_delete"
    other = "proj_round15_other"
    sid = "sess-round15-delete"

    # Disk truth: two messages, the NEWER draft, and the chat re-filed under B.
    disk = models.Session(
        session_id=sid,
        workspace="/ws/round15",
        messages=_messages(2, "disk"),
        project_id=other,
    )
    disk.composer_draft = {"text": "newer draft"}
    disk.save()

    # The cache: a full, stale two-message entry that still belongs to A and
    # carries the OLDER draft.
    stale = models.Session(
        session_id=sid,
        workspace="/ws/round15",
        messages=_messages(2, "cache"),
        project_id=pid,
    )
    stale.composer_draft = {"text": "older draft"}
    config.SESSIONS[sid] = stale

    # Deleting project A: the cached entry matches, so the clear collects the id
    # and the write-through is asked to unlink it. It must refuse.
    assert _delete_unlink(routes, pid, sid) == 0

    payload = _sidecar(session_dir, sid)
    assert payload["project_id"] == other, (
        "the chat had already been re-filed under another project; the delete "
        "cleared it from the cache's older copy"
    )
    assert payload["message_count"] == 2
    assert payload["composer_draft"] == {"text": "newer draft"}, (
        "the newer persisted draft was overwritten by the stale cache entry"
    )


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

    assert _delete_unlink(routes, pid, sid) == 1

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

    assert _delete_unlink(routes, pid, sid) == 0
    assert _sidecar(session_dir, sid)["project_id"] == pid


# ---------------------------------------------------------------------------
# Source guards — the shipped shape, not a copy of it
# ---------------------------------------------------------------------------


def test_write_through_resolves_through_the_sidecar_resolver():
    """``_persist_cleared_project_ids`` must not read the LRU directly."""
    body = _function_source("_persist_cleared_project_ids")
    assert _RESOLVER_CALL.search(body), (
        "the write-through must resolve each target through "
        "_delete_target_session so a stale cache entry is never saved back"
    )
    assert not _STALE_SOURCE.search(body), (
        "the write-through resolves its target straight from the cache again; "
        "that is the stale-object hole the maintainers reproduced twice"
    )
    # Ownership and streaming are re-checked on the REFRESHED object, BEFORE the
    # clear: a row re-filed under another project is never un-filed.
    resolve_i = _RESOLVER_CALL.search(body).start()
    stream_i = body.index("active_stream_id", resolve_i)
    proj_i = body.index('getattr(cached, "project_id", None)', resolve_i)
    clear_i = body.index("cached.project_id = None", resolve_i)
    assert resolve_i < proj_i < clear_i, (resolve_i, proj_i, clear_i)
    assert resolve_i < stream_i < clear_i, (resolve_i, stream_i, clear_i)


def test_delete_target_resolver_loads_the_sidecar_and_publishes_it():
    """The resolver's own rule: fresh sidecar first, cache only when ahead."""
    body = _function_source("_delete_target_session")
    assert "_Session.load(sid)" in body, (
        "the resolver must load the sidecar itself: get_session's freshness "
        "check is count-based and leaves an equal-count stale entry in place"
    )
    assert "SESSIONS[sid] = fresh" in body, (
        "the refreshed session must be published into the cache, or the delete "
        "handler's index pass can still fall back to the stale entry"
    )
    assert '"messages"' in body, (
        "the resolver must compare message counts to keep a genuinely newer "
        "cache entry (an unsaved draft)"
    )
    # The streaming deferral stays a pre-check, before the (full) load.
    assert body.index("active_stream_id") < body.index("_Session.load(sid)")
