from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pending_decisions import (
    create_pending_decision,
    get_pending_decision,
    list_pending_decisions,
    mark_pending_decision_resolved,
)


def decision() -> dict:
    return {
        "schema_version": "superset-pending-decision/v2",
        "decision_id": "decision-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "question": "这个修改应该只影响当前图表，还是所有共享数据集的消费者？" * 8,
        "options": ["target_local", "propagate_shared"],
        "impact_summary": {"shared_consumers": [900]},
        "state": "waiting_for_user",
        "hmac_sha256": "opaque-pico-signature",
    }


def test_pending_decision_survives_reload_without_expiry(tmp_path: Path) -> None:
    created = create_pending_decision(decision(), session_dir=tmp_path)
    loaded = get_pending_decision("session-1", "decision-1", session_dir=tmp_path)
    assert loaded == created
    assert "expires_at" not in loaded
    assert list_pending_decisions("session-1", session_dir=tmp_path) == [created]


def test_resolution_uses_sidecar_and_preserves_pico_signed_artifact(tmp_path: Path) -> None:
    create_pending_decision(decision(), session_dir=tmp_path)
    original_path = tmp_path / "_pending_decisions" / "session-1" / "decision-1.json"
    before = original_path.read_bytes()

    resolved = mark_pending_decision_resolved(
        "session-1",
        "decision-1",
        option="target_local",
        event_id="evt-resolution-1",
        turn_id="turn-2",
        session_dir=tmp_path,
    )

    assert resolved["state"] == "resolved"
    assert resolved["resolution"]["option"] == "target_local"
    assert original_path.read_bytes() == before
    assert list_pending_decisions("session-1", session_dir=tmp_path) == []


def test_resolution_rejects_wrong_session_stale_or_unoffered_option(tmp_path: Path) -> None:
    create_pending_decision(decision(), session_dir=tmp_path)
    with pytest.raises(ValueError, match="offered"):
        mark_pending_decision_resolved(
            "session-1", "decision-1", option="everything", event_id="evt-1", turn_id="turn-2",
            session_dir=tmp_path,
        )
    mark_pending_decision_resolved(
        "session-1", "decision-1", option="target_local", event_id="evt-1", turn_id="turn-2",
        session_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="active"):
        mark_pending_decision_resolved(
            "session-1", "decision-1", option="target_local", event_id="evt-2", turn_id="turn-3",
            session_dir=tmp_path,
        )


def test_contract_fixture_matches_shared_schema(tmp_path: Path) -> None:
    fixture = json.loads(Path("docs/contracts/intent-authorization-pending-decision-v2.json").read_text())
    assert fixture["schema_version"] == decision()["schema_version"]


def test_frontend_uses_normal_chat_start_without_timed_clarify() -> None:
    source = Path("static/messages.js").read_text(encoding="utf-8")
    durable_start = source.index("function showPendingDecisionCard")
    durable_end = source.index("function stopClarifyPollingForSession", durable_start)
    durable = source[durable_start:durable_end]
    assert "durable_decision" in durable
    assert "resolves_decision_id" in source
    assert "decision_option" in source
    assert "/api/chat/start" in source
    assert "/api/clarify/respond" not in durable
    assert "lockComposerForClarify" not in durable


def test_idle_session_load_starts_durable_decision_polling() -> None:
    source = Path("static/sessions.js").read_text(encoding="utf-8")
    load_start = source.index("async function loadSession")
    idle_start = source.index("    }else{\n      S.busy=false;", load_start)
    idle_end = source.index("_deferWorkspaceRefreshForSession(sid);", idle_start)
    assert "startClarifyPolling(sid)" in source[idle_start:idle_end]
