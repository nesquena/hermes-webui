"""#8950 round 2: compression-chain ownership (multi-hop + oversized metadata).

Validates that share-token continuation authorization correctly traces multi-hop
compression chains (A -> B -> C) and recovers authoritative lineage when cheap
metadata prefixes are incomplete (e.g. pushed past the prefix budget by an
oversized compression_anchor_summary).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from api import models, shares, streaming


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolate the session store and the public snapshot dir for one test."""
    session_dir = tmp_path / "sessions"
    share_dir = tmp_path / "shares"
    session_dir.mkdir()
    share_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(shares, "SHARES_DIR", share_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    return session_dir, share_dir


def _make_session(sid: str, title: str, text: str, workspace: Path, **kwargs) -> models.Session:
    session = models.Session(
        session_id=sid,
        title=title,
        workspace=str(workspace),
        messages=[{"role": "user", "content": text}],
        **kwargs,
    )
    session.save()
    return models.Session.load(sid)


def _seed_share_token(session_dir: Path, sid: str, token: str, created_at: float = 1000.0) -> models.Session:
    path = session_dir / f"{sid}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["share_token"] = token
    payload["share_created_at"] = created_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return models.Session.load(sid)


def _snapshot_bytes(share_dir: Path, token: str) -> bytes:
    return (share_dir / f"{token}.json").read_bytes()


def test_multi_hop_compression_chain_authorizes_revocation(store):
    """A legitimate multi-hop chain A -> B -> C authorizes tip C to revoke A's snapshot."""
    session_dir, share_dir = store
    sid_a = "sess-multi-a"
    sid_b = "sess-multi-b"
    sid_c = "sess-multi-c"

    # 1. Create root session A and publish a share snapshot.
    session_a = _make_session(sid_a, "Session A", "Root private content", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    # 2. Real migration A -> B.
    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    # 3. Real migration B -> C without refreshing the share.
    models.SESSIONS[sid_b] = session_b
    assert streaming._migrate_compression_session_ownership(
        session_b, sid_b, sid_c, agent_lock
    ) is True
    session_c = session_b
    session_c.save(touch_updated_at=False)

    assert session_c.session_id == sid_c
    assert session_c.parent_session_id == sid_b
    assert session_c.share_token == token

    # The snapshot on disk still records A as source_session_id.
    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["source_session_id"] == sid_a
    assert snapshot["revoked_at"] is None

    # Public read of A's link is still available prior to revocation.
    public = shares.load_share(token)
    assert public is not None
    assert public["title"] == "Session A"

    # Tip session C revokes the share.
    assert shares.revoke_share(session_c) is True

    # The share is now revoked on disk and public read returns None.
    snapshot_after = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot_after["revoked_at"] is not None
    assert shares.load_share(token) is None


def test_multi_hop_compression_chain_authorizes_refresh(store):
    """A legitimate multi-hop chain A -> B -> C allows tip C to refresh/rewrite the snapshot."""
    session_dir, share_dir = store
    sid_a = "sess-refresh-a"
    sid_b = "sess-refresh-b"
    sid_c = "sess-refresh-c"

    # 1. Root session A publishes a share.
    session_a = _make_session(sid_a, "Original Title", "Original text", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    # 2. Real migrations A -> B -> C.
    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    models.SESSIONS[sid_b] = session_b
    assert streaming._migrate_compression_session_ownership(
        session_b, sid_b, sid_c, agent_lock
    ) is True
    session_c = session_b
    session_c.title = "Updated Title from C"
    session_c.messages.append({"role": "user", "content": "New message in C"})
    session_c.save(touch_updated_at=False)

    # 3. Tip C refreshes the share.
    refreshed = shares.create_or_refresh_share(session_c)
    assert refreshed["token_reassigned"] is False
    assert refreshed["share_token"] == token

    # The snapshot is updated in place with C as owner and C's content.
    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["source_session_id"] == sid_c
    assert snapshot["title"] == "Updated Title from C"
    assert snapshot["revoked_at"] is None

    public = shares.load_share(token)
    assert public is not None
    assert public["title"] == "Updated Title from C"


def test_three_hop_compression_chain_authorizes_revocation(store):
    """Deep chain A -> B -> C -> D authorizes tip D to revoke."""
    session_dir, share_dir = store
    sids = ["sess-deep-a", "sess-deep-b", "sess-deep-c", "sess-deep-d"]

    session = _make_session(sids[0], "Deep Session", "Initial words", session_dir)
    created = shares.create_or_refresh_share(session)
    token = created["share_token"]
    session = _seed_share_token(session_dir, sids[0], token)

    agent_lock = threading.Lock()
    for old_sid, new_sid in zip(sids[:-1], sids[1:], strict=True):
        models.SESSIONS[old_sid] = session
        assert streaming._migrate_compression_session_ownership(
            session, old_sid, new_sid, agent_lock
        ) is True
        session.save(touch_updated_at=False)

    assert session.session_id == sids[3]
    assert session.share_token == token

    # Snapshot is still stamped with root session A.
    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["source_session_id"] == sids[0]

    # Revoke via tip D.
    assert shares.revoke_share(session) is True
    assert shares.load_share(token) is None


def test_oversized_metadata_recovers_authoritative_lineage(store):
    """Parent with compression_anchor_summary > 1MB recovers authoritative lineage."""
    session_dir, share_dir = store
    parent_sid = "sess-oversized-parent"
    child_sid = "sess-oversized-child"

    # Summary larger than _METADATA_PREFIX_MAX_BYTES pushes lineage keys past prefix cap.
    oversized_summary = "A" * (models._METADATA_PREFIX_MAX_BYTES + 4096)
    parent = _make_session(
        parent_sid,
        "Oversized Parent",
        "Parent content",
        session_dir,
        compression_anchor_summary=oversized_summary,
    )
    created = shares.create_or_refresh_share(parent)
    token = created["share_token"]
    parent = _seed_share_token(session_dir, parent_sid, token)

    # Migrate parent -> child via durable migration.
    models.SESSIONS[parent_sid] = parent
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        parent, parent_sid, child_sid, agent_lock
    ) is True
    child = parent
    child.save(touch_updated_at=False)

    # Prove that the cheap prefix is incomplete / None for parent.
    assert models._persisted_session_meta_prefix(parent_sid) is None

    # Prove authoritative metadata recovery works.
    parent_meta = models.Session.load_metadata_only(parent_sid)
    assert parent_meta is not None
    assert getattr(parent_meta, "pre_compression_snapshot", False) is True
    assert getattr(parent_meta, "pre_compression_continuation_session_id", None) == child_sid

    # Child must be authorized to revoke the share.
    assert shares.revoke_share(child) is True
    assert shares.load_share(token) is None


def test_oversized_metadata_authorizes_refresh(store):
    """Parent with oversized metadata allows child to refresh the snapshot."""
    session_dir, share_dir = store
    parent_sid = "sess-oversized-ref-parent"
    child_sid = "sess-oversized-ref-child"

    oversized_summary = "B" * (models._METADATA_PREFIX_MAX_BYTES + 4096)
    parent = _make_session(
        parent_sid,
        "Parent Title",
        "Parent content",
        session_dir,
        compression_anchor_summary=oversized_summary,
    )
    created = shares.create_or_refresh_share(parent)
    token = created["share_token"]
    parent = _seed_share_token(session_dir, parent_sid, token)

    models.SESSIONS[parent_sid] = parent
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        parent, parent_sid, child_sid, agent_lock
    ) is True
    child = parent
    child.title = "Refreshed by Child"
    child.save(touch_updated_at=False)

    refreshed = shares.create_or_refresh_share(child)
    assert refreshed["token_reassigned"] is False
    assert refreshed["share_token"] == token

    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["source_session_id"] == child_sid
    assert snapshot["title"] == "Refreshed by Child"


def test_broken_reciprocal_link_fails_closed(store):
    """A child whose parent points to another session (broken reciprocity) is rejected."""
    session_dir, share_dir = store
    sid_a = "sess-broken-a"
    sid_b = "sess-broken-b"
    sid_c = "sess-broken-c"

    session_a = _make_session(sid_a, "Session A", "Content A", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    # Migrate A -> B.
    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    # Craft child C that claims parent is B, but corrupt B's continuation to point elsewhere.
    session_c = _make_session(sid_c, "Session C", "Content C", session_dir, parent_session_id=sid_b)
    session_c = _seed_share_token(session_dir, sid_c, token)

    # Tamper with B's continuation link.
    path_b = session_dir / f"{sid_b}.json"
    payload_b = json.loads(path_b.read_text(encoding="utf-8"))
    payload_b["pre_compression_continuation_session_id"] = "sess-different-target"
    path_b.write_text(json.dumps(payload_b), encoding="utf-8")

    with pytest.raises(shares.ShareTokenOwnershipError) as exc_info:
        shares.revoke_share(session_c)
    assert sid_a in str(exc_info.value)
    assert sid_c in str(exc_info.value)

    # A's public link remains readable.
    assert shares.load_share(token) is not None


def test_parent_not_marked_snapshot_fails_closed(store):
    """If an intermediate node has pre_compression_snapshot=False, chain fails closed."""
    session_dir, share_dir = store
    sid_a = "sess-notsnap-a"
    sid_b = "sess-notsnap-b"
    sid_c = "sess-notsnap-c"

    session_a = _make_session(sid_a, "Session A", "Content A", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    session_c = _make_session(sid_c, "Session C", "Content C", session_dir, parent_session_id=sid_b)
    session_c = _seed_share_token(session_dir, sid_c, token)

    # Set B's pre_compression_snapshot to False.
    path_b = session_dir / f"{sid_b}.json"
    payload_b = json.loads(path_b.read_text(encoding="utf-8"))
    payload_b["pre_compression_snapshot"] = False
    path_b.write_text(json.dumps(payload_b), encoding="utf-8")

    with pytest.raises(shares.ShareTokenOwnershipError):
        shares.revoke_share(session_c)
    assert shares.load_share(token) is not None


def test_cyclic_compression_chain_fails_closed(store):
    """A cyclic lineage (e.g. C -> B -> C) is detected and rejected without infinite loop."""
    session_dir, share_dir = store
    sid_root = "sess-cycle-root"
    sid_b = "sess-cycle-b"
    sid_c = "sess-cycle-c"

    # Root publishes the share snapshot.
    session_root = _make_session(sid_root, "Root Session", "Root Content", session_dir)
    created = shares.create_or_refresh_share(session_root)
    token = created["share_token"]
    session_root = _seed_share_token(session_dir, sid_root, token)

    # B and C form a closed cycle pointing to each other.
    _make_session(
        sid_b, "Session B", "Content B", session_dir,
        parent_session_id=sid_c,
        pre_compression_snapshot=True,
        pre_compression_continuation_session_id=sid_c,
    )
    session_c = _make_session(
        sid_c, "Session C", "Content C", session_dir,
        parent_session_id=sid_b,
        pre_compression_snapshot=True,
        pre_compression_continuation_session_id=sid_b,
    )
    session_c = _seed_share_token(session_dir, sid_c, token)

    # Attempting to revoke root's share via cyclic chain fails closed.
    with pytest.raises(shares.ShareTokenOwnershipError) as exc_info:
        shares.revoke_share(session_c)
    assert sid_root in str(exc_info.value)
    assert sid_c in str(exc_info.value)

    # Root's share is untouched.
    assert shares.load_share(token) is not None


def test_unrelated_session_cannot_revoke_multi_hop_share(store):
    """An unrelated session claiming a token from a multi-hop chain is refused."""
    session_dir, share_dir = store
    sid_a = "sess-unrel-a"
    sid_b = "sess-unrel-b"
    sid_c = "sess-unrel-c"
    sid_thief = "sess-unrel-thief"

    session_a = _make_session(sid_a, "Session A", "Content A", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    models.SESSIONS[sid_b] = session_b
    assert streaming._migrate_compression_session_ownership(
        session_b, sid_b, sid_c, agent_lock
    ) is True
    session_c = session_b
    session_c.save(touch_updated_at=False)

    # Thief has no lineage link.
    thief = _make_session(sid_thief, "Thief", "Thief content", session_dir)
    thief = _seed_share_token(session_dir, sid_thief, token)

    with pytest.raises(shares.ShareTokenOwnershipError) as exc_info:
        shares.revoke_share(thief)
    assert sid_a in str(exc_info.value)
    assert sid_thief in str(exc_info.value)

    # Snapshot is untouched.
    assert shares.load_share(token) is not None


def test_revoke_share_owned_by_retains_exact_owner_rule(store):
    """revoke_share_owned_by retains exact owner matching and does not revoke continuation link."""
    session_dir, share_dir = store
    sid_parent = "sess-del-parent"
    sid_child = "sess-del-child"

    parent = _make_session(sid_parent, "Parent", "Parent text", session_dir)
    created = shares.create_or_refresh_share(parent)
    token = created["share_token"]
    parent = _seed_share_token(session_dir, sid_parent, token)

    models.SESSIONS[sid_parent] = parent
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        parent, sid_parent, sid_child, agent_lock
    ) is True
    child = parent
    child.save(touch_updated_at=False)

    # Child refreshes the share, making child the exact snapshot owner.
    shares.create_or_refresh_share(child)
    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["source_session_id"] == sid_child

    # Deleting the parent (calling revoke_share_owned_by with parent) must NOT revoke child's link.
    assert shares.revoke_share_owned_by(token, sid_parent) is True
    snapshot_after = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot_after["revoked_at"] is None
    assert shares.load_share(token) is not None

    # Deleting the child (exact owner) DOES revoke it.
    assert shares.revoke_share_owned_by(token, sid_child) is True
    snapshot_final = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot_final["revoked_at"] is not None
    assert shares.load_share(token) is None


# ---------------------------------------------------------------------------
# #8950 round 3: Malformed intermediate snapshot marker matrix
# ---------------------------------------------------------------------------


def _setup_tampered_intermediate_chain(
    store,
    prefix_id: str,
    marker_value,
    *,
    oversized: bool = False,
):
    session_dir, share_dir = store
    sid_a = f"sess-{prefix_id}-a"
    sid_b = f"sess-{prefix_id}-b"
    sid_c = f"sess-{prefix_id}-c"

    session_a = _make_session(sid_a, f"Session A ({prefix_id})", "Root private content", session_dir)
    created = shares.create_or_refresh_share(session_a)
    token = created["share_token"]
    session_a = _seed_share_token(session_dir, sid_a, token)

    models.SESSIONS[sid_a] = session_a
    agent_lock = threading.Lock()
    assert streaming._migrate_compression_session_ownership(
        session_a, sid_a, sid_b, agent_lock
    ) is True
    session_b = session_a
    session_b.save(touch_updated_at=False)

    models.SESSIONS[sid_b] = session_b
    assert streaming._migrate_compression_session_ownership(
        session_b, sid_b, sid_c, agent_lock
    ) is True
    session_c = session_b
    session_c.save(touch_updated_at=False)

    path_b = session_dir / f"{sid_b}.json"
    payload_b = json.loads(path_b.read_text(encoding="utf-8"))
    payload_b["pre_compression_snapshot"] = marker_value
    if oversized:
        payload_b["compression_anchor_summary"] = "X" * (models._METADATA_PREFIX_MAX_BYTES + 4096)
        path_b.write_text(json.dumps(payload_b), encoding="utf-8")
        assert models._persisted_session_meta_prefix(sid_b) is None
    else:
        path_b.write_text(json.dumps(payload_b), encoding="utf-8")
        assert models._persisted_session_meta_prefix(sid_b) is not None

    return session_c, token, sid_a, sid_c, share_dir


def _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir):
    with pytest.raises(shares.ShareTokenOwnershipError) as exc_info:
        shares.revoke_share(session_c)
    assert sid_a in str(exc_info.value)
    assert sid_c in str(exc_info.value)

    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["revoked_at"] is None
    assert shares.load_share(token) is not None


def test_intermediate_snapshot_marker_string_false_refuses_revocation(store):
    """Cheap-prefix path: string 'false' as intermediate snapshot marker refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "pfx-str-false", "false", oversized=False
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_intermediate_snapshot_marker_int_one_refuses_revocation(store):
    """Cheap-prefix path: integer 1 as intermediate snapshot marker refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "pfx-int-one", 1, oversized=False
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_intermediate_snapshot_marker_non_empty_object_refuses_revocation(store):
    """Cheap-prefix path: non-empty object as intermediate snapshot marker refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "pfx-obj", {"corrupted": True}, oversized=False
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_intermediate_snapshot_marker_empty_list_control_refuses_revocation(store):
    """Cheap-prefix path: control empty list [] as intermediate snapshot marker refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "pfx-ctrl-list", [], oversized=False
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_intermediate_snapshot_marker_boolean_false_control_refuses_revocation(store):
    """Cheap-prefix path: control boolean False as intermediate snapshot marker refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "pfx-ctrl-false", False, oversized=False
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_oversized_intermediate_snapshot_marker_string_false_refuses_revocation(store):
    """Full-read fallback: string 'false' with oversized metadata refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "ovsz-str-false", "false", oversized=True
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_oversized_intermediate_snapshot_marker_int_one_refuses_revocation(store):
    """Full-read fallback: integer 1 with oversized metadata refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "ovsz-int-one", 1, oversized=True
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_oversized_intermediate_snapshot_marker_non_empty_object_refuses_revocation(store):
    """Full-read fallback: non-empty object with oversized metadata refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "ovsz-obj", {"corrupted": True}, oversized=True
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_oversized_intermediate_snapshot_marker_empty_list_control_refuses_revocation(store):
    """Full-read fallback: control empty list [] with oversized metadata refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "ovsz-ctrl-list", [], oversized=True
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)


def test_oversized_intermediate_snapshot_marker_boolean_false_control_refuses_revocation(store):
    """Full-read fallback: control boolean False with oversized metadata refuses revocation."""
    session_c, token, sid_a, sid_c, share_dir = _setup_tampered_intermediate_chain(
        store, "ovsz-ctrl-false", False, oversized=True
    )
    _assert_revocation_refused_for_tampered_chain(session_c, token, sid_a, sid_c, share_dir)

