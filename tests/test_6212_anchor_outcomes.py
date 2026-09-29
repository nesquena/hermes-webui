"""Tests for #6212 reconstructing anchor outcomes from run journals."""

import pytest
from unittest.mock import patch
from api.routes import _run_journal_live_snapshot


def test_reconstruct_anchor_outcomes_zero_event_id_and_no_raw_payload():
    fake_events = [
        {
            "event": "artifact_reference",
            "event_id": 0,
            "payload": {
                "kind": "workspace_file",
                "path": "hello.py",
                "internal_secret": "do_not_leak",
            },
        },
        {
            "event": "state_saved",
            "event_id": 0,
            "payload": {
                "kind": "config",
                "name": "model_pref",
                "action": "updated",
                "raw_env": "SECRET=1",
            },
        },
    ]

    with patch("api.routes.find_run_summary", return_value={"session_id": "sess-123"}):
        with patch("api.routes.read_run_events", return_value={"events": fake_events}):
            snapshot = _run_journal_live_snapshot("stream-123")
            assert snapshot is not None
            scene = snapshot.get("anchor_activity_scene")
            assert scene is not None

            artifacts = scene.get("artifacts", [])
            assert len(artifacts) == 1
            art = artifacts[0]
            assert art["kind"] == "workspace_file"
            assert art["path"] == "hello.py"
            assert art["source_event_type"] == "artifact_reference"
            assert art["event_id"] == "0"
            assert "payload" not in art
            assert "internal_secret" not in art

            side_effects = scene.get("side_effects", [])
            assert len(side_effects) == 1
            se = side_effects[0]
            assert se["kind"] == "config"
            assert se["name"] == "model_pref"
            assert se["action"] == "updated"
            assert se["source_event_type"] == "state_saved"
            assert se["event_id"] == "0"
            assert "payload" not in se
            assert "raw_env" not in se
