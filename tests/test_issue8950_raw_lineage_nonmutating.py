"""#8950 LINEAGE-RAW-F1: ownership validation must not repair the evidence.

The lineage fallback used to decide share ownership through
``Session.load_metadata_only``. That constructor coerces
``pre_compression_snapshot`` with ``bool()`` and repairs adjacent duplicate
``_partial`` replies, saving the sidecar back. Composed with an oversized
metadata prefix and a malformed string marker, the ownership check therefore
rewrote the ancestor: the marker became JSON ``true`` and the two ``_partial``
replies collapsed into one, before the raw-marker observation.

These regressions pin two properties at once:

* the composed revoke/refresh must refuse (or reassign) exactly as it did
  before, and
* the ancestor sidecar bytes must be *unchanged* — an ownership check reads,
  it does not repair.
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


_DUPLICATE_PARTIALS = (
    {"role": "assistant", "content": "processando o pedido", "_partial": True},
    {"role": "assistant", "content": "processando o pedido", "_partial": True},
)


def _build_chain(store, prefix_id: str, marker_value, *, oversized: bool):
    """Root -> middle -> tip chain with a tampered middle sidecar.

    Returns (session_c, token, sid_a, sid_c, share_dir, path_b).
    """
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
    payload_b["messages"] = list(payload_b.get("messages") or []) + list(_DUPLICATE_PARTIALS)
    if oversized:
        payload_b["compression_anchor_summary"] = "X" * (models._METADATA_PREFIX_MAX_BYTES + 4096)
    path_b.write_text(json.dumps(payload_b), encoding="utf-8")

    # The composed precondition: the cheap prefix must be unusable, otherwise
    # the nonmutating fallback is never exercised.
    if oversized:
        assert models._persisted_session_meta_prefix(sid_b) is None
    else:
        assert models._persisted_session_meta_prefix(sid_b) is not None

    return session_c, token, sid_a, sid_c, share_dir, path_b


def _middle_evidence(path_b: Path) -> tuple[bytes, object, int]:
    raw = path_b.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    partials = [m for m in payload.get("messages") or [] if isinstance(m, dict) and m.get("_partial")]
    return raw, payload.get("pre_compression_snapshot"), len(partials)


def _assert_ancestor_untouched(path_b: Path, before: tuple[bytes, object, int]) -> None:
    raw_before, marker_before, partials_before = before
    raw_after, marker_after, partials_after = _middle_evidence(path_b)
    assert raw_after == raw_before, "the ownership check rewrote the ancestor sidecar"
    assert marker_after == marker_before, "the raw marker was coerced and persisted"
    assert partials_after == partials_before == 2, "the duplicate _partial replies were repaired by the read"


@pytest.mark.parametrize("oversized", [True, False], ids=["oversized-full-read", "cheap-prefix"])
def test_malformed_marker_with_duplicate_partials_refuses_revocation(store, oversized):
    """Composed case: malformed marker + duplicate partials still refuses, silently."""
    session_c, token, sid_a, sid_c, share_dir, path_b = _build_chain(
        store, f"rawrev-{int(oversized)}", "false", oversized=oversized
    )
    before = _middle_evidence(path_b)

    with pytest.raises(shares.ShareTokenOwnershipError) as exc_info:
        shares.revoke_share(session_c)
    assert sid_a in str(exc_info.value)
    assert sid_c in str(exc_info.value)

    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["revoked_at"] is None
    assert shares.load_share(token) is not None
    _assert_ancestor_untouched(path_b, before)


@pytest.mark.parametrize("oversized", [True, False], ids=["oversized-full-read", "cheap-prefix"])
def test_malformed_marker_with_duplicate_partials_reassigns_on_refresh(store, oversized):
    """Composed case on refresh: the tip gets its own token, the ancestor is not rewritten."""
    session_c, token, sid_a, sid_c, share_dir, path_b = _build_chain(
        store, f"rawref-{int(oversized)}", "false", oversized=oversized
    )
    before = _middle_evidence(path_b)
    snapshot_before = (share_dir / f"{token}.json").read_bytes()

    refreshed = shares.create_or_refresh_share(session_c)
    assert refreshed["token_reassigned"] is True
    assert refreshed["share_token"] != token

    assert (share_dir / f"{token}.json").read_bytes() == snapshot_before
    _assert_ancestor_untouched(path_b, before)


def test_valid_boolean_marker_with_duplicate_partials_still_authorizes(store):
    """Control: a genuine boolean marker still authorizes revoke — and still mutates nothing."""
    session_c, token, sid_a, sid_c, share_dir, path_b = _build_chain(
        store, "rawvalid", True, oversized=True
    )
    before = _middle_evidence(path_b)
    _, marker_before, _ = before
    assert marker_before is True

    result = shares.revoke_share(session_c)
    assert result is not None

    snapshot = json.loads((share_dir / f"{token}.json").read_text(encoding="utf-8"))
    assert snapshot["revoked_at"] is not None
    _assert_ancestor_untouched(path_b, before)


def test_normal_save_control_leaves_ancestor_untouched(store):
    """Control: an unrelated save of the tip never repairs the middle sidecar."""
    session_c, token, sid_a, sid_c, share_dir, path_b = _build_chain(
        store, "rawsave", "false", oversized=True
    )
    before = _middle_evidence(path_b)

    session_c.save(touch_updated_at=False, skip_index=True)

    _assert_ancestor_untouched(path_b, before)
