"""Regression tests for #7530 session toolset cache invalidation."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
from api.models import new_session
from api.routes import handle_post
from tests.test_issue4490_presession_toolsets import _DummyHandler


pytestmark = pytest.mark.skipif(
    config._AGENT_DIR is None,
    reason="hermes-agent not found",
)


def test_toolset_change_fails_closed_when_system_prompt_invalidation_fails(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", collections.OrderedDict()),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save(skip_index=True)

        sidecar = session_dir / f"{session.session_id}.json"
        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["old-toolset"]

        db = Mock()
        db.update_system_prompt.side_effect = RuntimeError("prompt invalidation failed")

        with (
            patch("api.routes.get_session", return_value=session),
            patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
            patch("hermes_state.SessionDB", return_value=db),
        ):
            handler = _DummyHandler({
                "session_id": session.session_id,
                "toolsets": ["new-toolset"],
            })

            with pytest.raises(RuntimeError, match="prompt invalidation failed"):
                handle_post(handler, urlparse("/api/session/toolsets"))

        persisted = json.loads(sidecar.read_text())

        assert persisted["enabled_toolsets"] == ["old-toolset"]
        assert session.enabled_toolsets == ["old-toolset"]
        db.update_system_prompt.assert_called_once_with(session.session_id, None)
        db.update_session_tool_names.assert_not_called()
        db.close.assert_called_once()


def test_toolset_change_fails_closed_when_tool_names_invalidation_fails(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", collections.OrderedDict()),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save(skip_index=True)

        sidecar = session_dir / f"{session.session_id}.json"
        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["old-toolset"]

        db = Mock()
        db.update_session_tool_names.side_effect = RuntimeError(
            "tool names invalidation failed"
        )

        with (
            patch("api.routes.get_session", return_value=session),
            patch(
                "api.routes._active_state_db_path",
                return_value=tmp_path / "state.db",
            ),
            patch("hermes_state.SessionDB", return_value=db),
        ):
            handler = _DummyHandler({
                "session_id": session.session_id,
                "toolsets": ["new-toolset"],
            })

            with pytest.raises(RuntimeError, match="tool names invalidation failed"):
                handle_post(handler, urlparse("/api/session/toolsets"))

        persisted = json.loads(sidecar.read_text())

        assert persisted["enabled_toolsets"] == ["old-toolset"]
        assert session.enabled_toolsets == ["old-toolset"]
        db.update_system_prompt.assert_called_once_with(session.session_id, None)
        db.update_session_tool_names.assert_called_once_with(session.session_id, None)
        db.close.assert_called_once()


def test_toolset_change_invalidates_both_caches_before_saving_override(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", collections.OrderedDict()),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save(skip_index=True)

        sidecar = session_dir / f"{session.session_id}.json"

        calls = []
        db = Mock()
        db.update_system_prompt.side_effect = (
            lambda sid, value: calls.append(("system_prompt", sid, value))
        )
        db.update_session_tool_names.side_effect = (
            lambda sid, value: calls.append(("tool_names", sid, value))
        )

        with (
            patch("api.routes.get_session", return_value=session),
            patch(
                "api.routes._active_state_db_path",
                return_value=tmp_path / "state.db",
            ),
            patch("hermes_state.SessionDB", return_value=db),
        ):
            handler = _DummyHandler({
                "session_id": session.session_id,
                "toolsets": ["new-toolset"],
            })

            handle_post(handler, urlparse("/api/session/toolsets"))

        persisted = json.loads(sidecar.read_text())
        payload = handler.payload()

        assert calls == [
            ("system_prompt", session.session_id, None),
            ("tool_names", session.session_id, None),
        ]
        assert persisted["enabled_toolsets"] == ["new-toolset"]
        assert session.enabled_toolsets == ["new-toolset"]

        assert handler.status == 200
        assert payload == {
            "ok": True,
            "enabled_toolsets": ["new-toolset"],
        }

        db.close.assert_called_once()


def test_toolset_change_survives_restart_with_fresh_tools_and_prompt(
    tmp_path, monkeypatch
):
    """A restarted Agent must rebuild tools and prompt from the new toolset."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # api.config normally makes the discovered Hermes Agent importable.
    # Keep that production ordering here as well: append, never prepend.
    assert config._AGENT_DIR is not None
    agent_dir = str(config._AGENT_DIR)
    if agent_dir not in sys.path:
        sys.path.append(agent_dir)

    from hermes_state import SessionDB
    from run_agent import AIAgent
    from agent.system_prompt import build_system_prompt

    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", collections.OrderedDict()),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save(skip_index=True)
        sid = session.session_id

        # Seed the Agent DB with the stale pins that caused #7530.
        db = SessionDB(state_db)
        try:
            db.create_session(
                sid,
                source="webui",
                model="test-model",
            )
            db.update_system_prompt(sid, "STALE_PROMPT_MARKER")
            db.update_session_tool_names(sid, ["stale_tool"])

            before = db.get_session(sid)
            assert before is not None
            assert before["system_prompt_hash"] is not None
            assert before["system_prompt"] == "STALE_PROMPT_MARKER"
            assert json.loads(before["tool_names"]) == ["stale_tool"]
        finally:
            db.close()

        # Change the per-session toolset through the real route.
        with (
            patch("api.routes.get_session", return_value=session),
            patch(
                "api.routes._active_state_db_path",
                return_value=state_db,
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["terminal", "clarify"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 200

        # Both Agent-side cache pins must be cold before the new sidecar
        # override is allowed to become authoritative.
        db = SessionDB(state_db)
        try:
            cleared = db.get_session(sid)
            assert cleared is not None
            assert cleared["system_prompt_hash"] is None
            assert cleared["system_prompt"] is None
            assert cleared["tool_names"] is None
        finally:
            db.close()

        # Simulate a WebUI restart: discard the in-memory Session and reload
        # the durable sidecar.
        models.SESSIONS.clear()
        restarted = models.Session.load_metadata_only(sid)

        assert restarted is not None
        assert restarted.enabled_toolsets == ["terminal", "clarify"]

        # Construct the next real Agent from the restarted session override.
        # Client construction is local; no model request is made.
        agent = AIAgent(
            model="test-model",
            provider="openai",
            api_key="test-key",
            base_url="http://127.0.0.1:9/v1",
            enabled_toolsets=restarted.enabled_toolsets,
            session_id=sid,
            quiet_mode=True,
            skip_memory=True,
            skip_background_review=True,
            skip_context_files=True,
        )

        assert "terminal" in agent.valid_tool_names
        assert "clarify" in agent.valid_tool_names
        assert "stale_tool" not in agent.valid_tool_names

        # The prompt must be generated from this freshly constructed Agent,
        # not restored from the stale DB snapshot.
        rebuilt_prompt = build_system_prompt(agent)

        assert rebuilt_prompt
        assert "STALE_PROMPT_MARKER" not in rebuilt_prompt
        assert agent._cached_system_prompt_static
