"""Behavioral contract for stable WebUI compression lineage delivery."""
from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path

import pytest

from api import config, routes
import api.models as models


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
    **extra,
) -> None:
    payload = {
        "session_id": session_id,
        "profile": profile,
        "parent_session_id": parent_session_id,
        "pre_compression_snapshot": pre_compression_snapshot,
        "session_source": "webui",
        "messages": [],
        **extra,
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


@pytest.mark.parametrize(
    "boundary_marker",
    [
        {"session_source": "fork"},
        {"relationship_type": "child_session"},
    ],
)
def test_non_compression_child_keeps_one_stable_compression_root(
    tmp_path,
    boundary_marker,
):
    """C8: inherited fork/child metadata must not advance the root per rotation."""
    lineage = _lineage_module()
    _write_session(tmp_path, "parent", pre_compression_snapshot=True)
    for session_id, parent_id, snapshot in (
        ("childroot", "parent", True),
        ("childmiddle", "childroot", True),
        ("childtip", "childmiddle", False),
    ):
        _write_session(
            tmp_path,
            session_id,
            parent_session_id=parent_id,
            pre_compression_snapshot=snapshot,
            **boundary_marker,
        )
    for previous_tip, delivery in (
        ("childroot", "childmiddle"),
        ("childmiddle", "childtip"),
    ):
        lineage.record_lineage_transition(
            root_session_id="childroot",
            previous_tip_session_id=previous_tip,
            delivery_session_id=delivery,
            profile="default",
            state="committed",
            session_dir=tmp_path,
        )

    for requested in ("childroot", "childmiddle", "childtip"):
        resolved = lineage.resolve_session_lineage(requested, session_dir=tmp_path)
        assert resolved.root_session_id == "childroot"
        assert resolved.delivery_session_id == "childtip"
        assert resolved.hop_count == 2

    _write_session(
        tmp_path,
        "unrelatedchild",
        parent_session_id="childroot",
        **boundary_marker,
    )
    unrelated = lineage.resolve_session_lineage(
        "unrelatedchild",
        session_dir=tmp_path,
    )
    assert unrelated.root_session_id == "unrelatedchild"
    assert unrelated.delivery_session_id == "unrelatedchild"
    assert unrelated.hop_count == 0


def _completion_event(kind: str, completion_id: str, *, profile: str = "default") -> dict:
    event = {
        "type": "async_delegation" if kind == "async_delegation" else "process_complete",
        "session_key": "webui:root",
        "origin_ui_session_id": "root",
        "origin_profile": profile,
        "output": "payload must not be copied into the receipt",
    }
    event["delegation_id" if kind == "async_delegation" else "process_id"] = completion_id
    return event


def _write_two_segment_completion_lineage(session_dir: Path) -> None:
    _write_session(session_dir, "root", pre_compression_snapshot=True)
    _write_session(session_dir, "tip", parent_session_id="root")


def test_completion_receipt_is_single_owner_restart_safe_and_prompt_free(tmp_path):
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-1"),
        "root",
        session_dir=tmp_path,
    )

    assert context.completion_key == "process:proc-1"
    assert context.root_session_id == "root"
    assert context.delivery_session_id == "tip"
    assert context.profile == "default"
    expected_correlation = hashlib.sha256(b"process:proc-1").hexdigest()
    assert context.correlation_sha256 == expected_correlation
    assert context.turn_id == f"completion-{expected_correlation[:32]}"

    first = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    assert first is not None
    document = json.loads(first.receipt_path.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert list(document["receipts"]) == [context.completion_key]
    receipt = document["receipts"][context.completion_key]
    assert receipt["state"] == "accepted"
    assert receipt["owner_token"] == first.owner_token
    assert receipt["attempt"] == first.attempt == 1
    assert receipt["reservation_id"] == first.reservation_id
    assert receipt["lineage_id"] == "root"
    assert receipt["origin_session_id"] == "root"
    assert receipt["delivery_session_id"] == "tip"
    assert receipt["correlation_id"] == expected_correlation
    assert receipt["turn_id"] == context.turn_id
    assert "output" not in receipt
    assert "prompt" not in receipt
    assert "wakeup_prompt" not in receipt

    with pytest.raises(lineage.CompletionDeliveryBusyError):
        lineage.claim_completion_delivery(context, session_dir=tmp_path)

    lineage.release_completion_delivery_claim(first)
    retried = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    assert retried is not None
    assert retried.state == "accepted"
    assert retried.attempt == 2
    assert retried.owner_token != first.owner_token
    lineage.mark_completion_incorporated(retried, session_dir=tmp_path)
    lineage.release_completion_delivery_claim(retried)

    assert lineage.claim_completion_delivery(context, session_dir=tmp_path) is None
    assert lineage.read_completion_delivery_receipt(
        context, session_dir=tmp_path
    )["state"] == "incorporated"
    final_document = json.loads(first.receipt_path.read_text(encoding="utf-8"))
    final_receipt = final_document["receipts"][context.completion_key]
    assert final_receipt["attempt"] == 2
    assert final_receipt["incorporated_at"] >= final_receipt["accepted_at"]
    assert first.receipt_path.name == "_completion_delivery_receipts.json"
    assert first.receipt_path.stat().st_mode & 0o777 == 0o600


def test_restart_scan_checks_terminal_receipt_before_rebinding_delivery_tip(tmp_path):
    """C1: incorporated work stays terminal after its original tip rotates."""
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-terminal-before-rebind"),
        "root",
        session_dir=tmp_path,
    )
    claim = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    lineage.mark_completion_incorporated(claim, session_dir=tmp_path)
    lineage.release_completion_delivery_claim(claim)
    retry_context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-accepted-after-terminal"),
        "root",
        session_dir=tmp_path,
    )
    retry_claim = lineage.claim_completion_delivery(
        retry_context,
        session_dir=tmp_path,
    )
    lineage.release_completion_delivery_claim(retry_claim)

    _write_session(
        tmp_path,
        "tip",
        parent_session_id="root",
        pre_compression_snapshot=True,
    )
    _write_session(tmp_path, "newtip", parent_session_id="tip")
    lineage.record_lineage_transition(
        root_session_id="root",
        previous_tip_session_id="tip",
        delivery_session_id="newtip",
        profile="default",
        state="committed",
        session_dir=tmp_path,
    )

    [rebound] = lineage.accepted_completion_delivery_contexts(session_dir=tmp_path)
    assert rebound.completion_key == retry_context.completion_key
    assert rebound.delivery_session_id == "newtip"
    assert rebound.receipt_delivery_session_id == "tip"


def test_restart_scan_rebinds_only_accepted_receipt_and_keeps_persisted_identity(tmp_path):
    """C1: retry routing advances, while receipt CAS stays in its accepted domain."""
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-accepted-rebind"),
        "root",
        session_dir=tmp_path,
    )
    first = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    lineage.release_completion_delivery_claim(first)

    _write_session(
        tmp_path,
        "tip",
        parent_session_id="root",
        pre_compression_snapshot=True,
    )
    _write_session(tmp_path, "newtip", parent_session_id="tip")
    lineage.record_lineage_transition(
        root_session_id="root",
        previous_tip_session_id="tip",
        delivery_session_id="newtip",
        profile="default",
        state="committed",
        session_dir=tmp_path,
    )

    [rebound] = lineage.accepted_completion_delivery_contexts(session_dir=tmp_path)
    assert rebound.delivery_session_id == "newtip"
    assert rebound.receipt_delivery_session_id == "tip"
    retried = lineage.claim_completion_delivery(rebound, session_dir=tmp_path)
    assert retried.attempt == 2
    lineage.release_completion_delivery_claim(retried)
    durable = json.loads(first.receipt_path.read_text(encoding="utf-8"))
    assert durable["receipts"][context.completion_key]["delivery_session_id"] == "tip"


def test_completion_receipt_namespaces_process_and_delegation_ids(tmp_path):
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    process_context = lineage.build_completion_delivery_context(
        _completion_event("process", "same-id"),
        "root",
        session_dir=tmp_path,
    )
    delegation_context = lineage.build_completion_delivery_context(
        _completion_event("async_delegation", "same-id"),
        "root",
        session_dir=tmp_path,
    )

    assert process_context.completion_key == "process:same-id"
    assert delegation_context.completion_key == "async_delegation:same-id"
    assert process_context.correlation_sha256 != delegation_context.correlation_sha256
    assert process_context.turn_id != delegation_context.turn_id


def test_malformed_completion_receipt_fails_closed_and_remains_visible(tmp_path):
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-corrupt"),
        "root",
        session_dir=tmp_path,
    )
    claim = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    receipt_path = claim.receipt_path
    lineage.release_completion_delivery_claim(claim)
    receipt_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(lineage.CompletionDeliveryReceiptError, match="malformed"):
        lineage.claim_completion_delivery(context, session_dir=tmp_path)
    assert receipt_path.read_text(encoding="utf-8") == "not-json"


def test_conflicting_completion_receipt_identity_fails_closed(tmp_path):
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-conflict"),
        "root",
        session_dir=tmp_path,
    )
    claim = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    receipt_path = claim.receipt_path
    lineage.release_completion_delivery_claim(claim)
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["receipts"][context.completion_key]["delivery_session_id"] = "root"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(lineage.CompletionDeliveryReceiptError, match="conflicting"):
        lineage.claim_completion_delivery(context, session_dir=tmp_path)


def test_tampered_incorporated_receipt_still_fails_restart_scan_closed(tmp_path):
    """C1: terminal state skips routing only after immutable identity validation."""
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)
    context = lineage.build_completion_delivery_context(
        _completion_event("process", "proc-tampered-incorporated"),
        "root",
        session_dir=tmp_path,
    )
    claim = lineage.claim_completion_delivery(context, session_dir=tmp_path)
    lineage.mark_completion_incorporated(claim, session_dir=tmp_path)
    receipt_path = claim.receipt_path
    lineage.release_completion_delivery_claim(claim)
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["receipts"][context.completion_key]["correlation_id"] = "0" * 64
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(lineage.CompletionDeliveryReceiptError, match="conflicting"):
        lineage.accepted_completion_delivery_contexts(session_dir=tmp_path)


def test_completion_context_rejects_cross_profile_origin(tmp_path):
    lineage = _lineage_module()
    _write_two_segment_completion_lineage(tmp_path)

    with pytest.raises(lineage.LineageResolutionError, match="cross-profile"):
        lineage.build_completion_delivery_context(
            _completion_event("process", "proc-profile", profile="other"),
            "root",
            session_dir=tmp_path,
        )


def test_identical_text_distinct_completion_ids_remain_distinct(tmp_path, monkeypatch):
    lineage = _lineage_module()
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    session = models.Session(session_id="same-text", title="same", profile="default")
    session.save()
    captured = []

    class ParkedThread:
        def __init__(self, *args, **kwargs):
            self.admission = kwargs["kwargs"]["admission"]
            captured.append(self.admission)

        def start(self):
            self.admission.admitted.set()

    monkeypatch.setattr(routes.threading, "Thread", ParkedThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)

    contexts = []
    for completion_id in ("proc-a", "proc-b"):
        context = lineage.build_completion_delivery_context(
            {
                "type": "process_complete",
                "process_id": completion_id,
                "session_key": "ui:same-text",
                "origin_ui_session_id": "same-text",
                "origin_profile": "default",
            },
            "same-text",
            session_dir=tmp_path,
        )
        contexts.append(context)
        response = routes._start_chat_stream_for_session(
            session,
            msg="identical completion text",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider="test-provider",
            source="process_wakeup",
            external_runtime_owned=False,
            completion_context=context,
        )
        assert int(response.get("_status", 200)) == 200
        routes._abort_prepared_chat_turn(session, response["stream_id"], captured[-1])

    persisted = json.loads((tmp_path / "same-text.json").read_text(encoding="utf-8"))
    expected_metadata = [lineage.completion_delivery_metadata(item) for item in contexts]
    assert [row["_completion_delivery"] for row in persisted["messages"]] == expected_metadata
    assert [row["_completion_delivery"] for row in persisted["context_messages"]] == expected_metadata
    receipt_document = json.loads(
        (tmp_path / "_completion_delivery_receipts.json").read_text(encoding="utf-8")
    )
    assert list(receipt_document["receipts"]) == [
        context.completion_key for context in contexts
    ]


def test_session_save_refusal_cannot_validate_previous_sidecar(tmp_path, monkeypatch):
    """Y2: a refused active/pending save must raise, never echo stale bytes."""
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    original = models.Session(
        session_id="stale-readback",
        title="original",
        profile="default",
        messages=[{"role": "user", "content": "already durable"}],
    )
    original.save(skip_index=True)
    before = original.path.read_bytes()

    stale = models.Session(
        session_id="stale-readback",
        title="stale",
        profile="default",
        messages=[],
        active_stream_id="reservation",
        pending_user_message="not persisted",
    )
    with pytest.raises(RuntimeError, match="refusing to overwrite session"):
        stale.save(skip_index=True)
    assert stale.path.read_bytes() == before
