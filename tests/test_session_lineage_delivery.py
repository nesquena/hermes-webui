"""Behavioral contract for stable WebUI compression lineage delivery."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _lineage_module():
    try:
        return importlib.import_module("api.session_lineage")
    except ModuleNotFoundError:
        pytest.fail(
            "stable lineage resolver unavailable: oldest-root/current-tip "
            "coordination is not implemented"
        )


def _write_session(
    session_dir: Path,
    session_id: str,
    *,
    parent_session_id: str | None = None,
    profile: str | None = "default",
    pre_compression_snapshot: bool = False,
) -> None:
    payload = {
        "session_id": session_id,
        "profile": profile,
        "parent_session_id": parent_session_id,
        "pre_compression_snapshot": pre_compression_snapshot,
        "session_source": "webui",
        "messages": [],
    }
    (session_dir / f"{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_verified_compression_chain_resolves_oldest_root_and_current_tip(tmp_path):
    lineage = _lineage_module()
    _write_session(tmp_path, "root", pre_compression_snapshot=True)
    _write_session(
        tmp_path,
        "middle",
        parent_session_id="root",
        pre_compression_snapshot=True,
    )
    _write_session(tmp_path, "tip", parent_session_id="middle")

    lineage.record_lineage_transition(
        root_session_id="root",
        previous_tip_session_id="middle",
        delivery_session_id="tip",
        profile="default",
        state="pending",
        session_dir=tmp_path,
    )
    with pytest.raises(lineage.LineageResolutionError, match="pending"):
        lineage.resolve_session_lineage("root", session_dir=tmp_path)
    lineage.record_lineage_transition(
        root_session_id="root",
        previous_tip_session_id="middle",
        delivery_session_id="tip",
        profile="default",
        state="committed",
        session_dir=tmp_path,
    )

    for requested in ("root", "middle", "tip"):
        resolved = lineage.resolve_session_lineage(
            requested,
            session_dir=tmp_path,
            expected_profile="default",
        )
        assert resolved.root_session_id == "root"
        assert resolved.delivery_session_id == "tip"
        assert resolved.profile == "default"
        assert resolved.hop_count == 2


def test_pending_fork_cycle_and_cross_profile_lineage_fail_closed(tmp_path):
    lineage = _lineage_module()

    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    _write_session(pending_dir, "pendingroot", pre_compression_snapshot=True)
    _write_session(pending_dir, "pendingtip", parent_session_id="pendingroot")
    lineage.record_lineage_transition(
        root_session_id="pendingroot",
        previous_tip_session_id="pendingroot",
        delivery_session_id="pendingtip",
        profile="default",
        state="pending",
        session_dir=pending_dir,
    )
    with pytest.raises(lineage.LineageResolutionError, match="pending"):
        lineage.resolve_session_lineage("pendingroot", session_dir=pending_dir)

    fork_dir = tmp_path / "fork"
    fork_dir.mkdir()
    _write_session(fork_dir, "forkroot", pre_compression_snapshot=True)
    _write_session(fork_dir, "forktipa", parent_session_id="forkroot")
    _write_session(fork_dir, "forktipb", parent_session_id="forkroot")
    with pytest.raises(lineage.LineageResolutionError, match="fork"):
        lineage.resolve_session_lineage("forkroot", session_dir=fork_dir)

    cycle_dir = tmp_path / "cycle"
    cycle_dir.mkdir()
    _write_session(
        cycle_dir,
        "cyclea",
        parent_session_id="cycleb",
        pre_compression_snapshot=True,
    )
    _write_session(
        cycle_dir,
        "cycleb",
        parent_session_id="cyclea",
        pre_compression_snapshot=True,
    )
    with pytest.raises(lineage.LineageResolutionError, match="cycle"):
        lineage.resolve_session_lineage("cyclea", session_dir=cycle_dir)

    cross_profile_dir = tmp_path / "cross-profile"
    cross_profile_dir.mkdir()
    _write_session(
        cross_profile_dir,
        "profileroot",
        profile="default",
        pre_compression_snapshot=True,
    )
    _write_session(
        cross_profile_dir,
        "profiletip",
        parent_session_id="profileroot",
        profile="other",
    )
    with pytest.raises(lineage.LineageResolutionError, match="cross-profile"):
        lineage.resolve_session_lineage(
            "profileroot",
            session_dir=cross_profile_dir,
            expected_profile="default",
        )

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    _write_session(
        missing_dir,
        "missingtwo",
        parent_session_id="missingone",
        pre_compression_snapshot=True,
    )
    _write_session(missing_dir, "missingtip", parent_session_id="missingtwo")
    with pytest.raises(lineage.LineageResolutionError, match="missing"):
        lineage.resolve_session_lineage("missingtip", session_dir=missing_dir)

    long_dir = tmp_path / "long"
    long_dir.mkdir()
    for index in range(22):
        _write_session(
            long_dir,
            f"segment{index}",
            parent_session_id=f"segment{index - 1}" if index else None,
            pre_compression_snapshot=index < 21,
        )
    with pytest.raises(lineage.LineageResolutionError, match="20"):
        lineage.resolve_session_lineage("segment21", session_dir=long_dir)
