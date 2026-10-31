"""Regression tests for #7530 session toolset cache invalidation."""

from __future__ import annotations

import collections
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
import api.routes as routes
from api.models import new_session
from api.routes import handle_post
from tests.test_issue4490_presession_toolsets import _DummyHandler



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
            patch.dict(sys.modules, {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))}),
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
            patch.dict(sys.modules, {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))}),
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
            patch.dict(sys.modules, {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))}),
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



def test_toolset_change_restores_shared_session_when_sidecar_save_fails(tmp_path):
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
        sid = session.session_id
        sidecar = session_dir / f"{sid}.json"
        previous_updated_at = session.updated_at

        calls = []
        db = Mock()
        db.update_system_prompt.side_effect = (
            lambda session_id, value: calls.append(("system_prompt", session_id, value))
        )
        db.update_session_tool_names.side_effect = (
            lambda session_id, value: calls.append(("tool_names", session_id, value))
        )
        real_safe_replace = models._safe_replace

        def fail_session_sidecar(src, dst):
            if Path(dst) == sidecar:
                raise OSError("session sidecar replace failed")
            return real_safe_replace(src, dst)

        with (
            patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
            patch.object(models, "_safe_replace", side_effect=fail_session_sidecar),
            patch.dict(
                sys.modules,
                {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))},
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })

            with pytest.raises(OSError, match="session sidecar replace failed"):
                handle_post(handler, urlparse("/api/session/toolsets"))

        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["old-toolset"]
        assert models.SESSIONS[sid].enabled_toolsets == ["old-toolset"]
        assert models.get_session(sid).enabled_toolsets == ["old-toolset"]
        assert session.updated_at == previous_updated_at
        assert handler.status != 200
        assert calls == [
            ("system_prompt", sid, None),
            ("tool_names", sid, None),
        ]
        db.close.assert_called_once()

        session.title = "Unrelated later save"
        session.save(skip_index=True)
        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["old-toolset"]


def test_toolset_change_keeps_committed_sidecar_after_index_save_fails(tmp_path):
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
        session.save()
        sid = session.session_id
        sidecar = session_dir / f"{sid}.json"
        old_index = json.loads(index_file.read_text())

        calls = []
        db = Mock()
        db.update_system_prompt.side_effect = (
            lambda session_id, value: calls.append(("system_prompt", session_id, value))
        )
        db.update_session_tool_names.side_effect = (
            lambda session_id, value: calls.append(("tool_names", session_id, value))
        )

        with (
            patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
            patch("api.routes._write_session_index", side_effect=OSError("session index replace failed")),
            patch.dict(
                sys.modules,
                {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))},
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 200
        assert handler.payload() == {
            "ok": True,
            "enabled_toolsets": ["new-toolset"],
        }
        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["new-toolset"]
        assert models.SESSIONS[sid].enabled_toolsets == ["new-toolset"]
        assert models.get_session(sid).enabled_toolsets == ["new-toolset"]
        assert json.loads(index_file.read_text()) == old_index
        assert calls == [
            ("system_prompt", sid, None),
            ("tool_names", sid, None),
        ]
        db.close.assert_called_once()

        # A later unrelated save must preserve the committed toolset selection.
        session.title = "Unrelated later save"
        session.save(skip_index=True)
        assert json.loads(sidecar.read_text())["enabled_toolsets"] == ["new-toolset"]

def test_toolset_change_is_rejected_during_active_turn(tmp_path):
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
        sid = session.session_id

        with (
            patch(
                "api.routes._active_stream_blocks_chat_start",
                return_value=True,
            ),
            patch(
                "api.routes._active_run_stream_for_session",
                side_effect=AssertionError("run probe must not be needed"),
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 409
        assert json.loads(
            (session_dir / f"{sid}.json").read_text()
        )["enabled_toolsets"] == ["old-toolset"]


def test_toolset_change_is_rejected_during_active_run_unwind(tmp_path):
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
        sid = session.session_id

        with (
            patch(
                "api.routes._active_stream_blocks_chat_start",
                return_value=False,
            ),
            patch(
                "api.routes._active_run_stream_for_session_fail_closed",
                return_value="unwinding-stream",
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 409
        assert json.loads(
            (session_dir / f"{sid}.json").read_text()
        )["enabled_toolsets"] == ["old-toolset"]


def test_toolset_change_rejects_old_cancelled_worker_without_age_pruning(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    registry = collections.OrderedDict()
    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", registry),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save(skip_index=True)
        sid = session.session_id
        stream_id = "delayed-cancelled-worker"

        config.ACTIVE_RUNS.clear()
        config.STREAMS.clear()
        config.register_active_run(
            stream_id,
            session_id=sid,
            phase="cancelling",
            cancelled_at=time.time() - 600.0,
        )

        try:
            with patch(
                "api.routes._active_state_db_path",
                side_effect=AssertionError("live worker must block before SessionDB"),
            ):
                handler = _DummyHandler({
                    "session_id": sid,
                    "toolsets": ["new-toolset"],
                })
                handle_post(handler, urlparse("/api/session/toolsets"))

            assert handler.status == 409
            assert json.loads(
                (session_dir / f"{sid}.json").read_text()
            )["enabled_toolsets"] == ["old-toolset"]
            assert stream_id in config.ACTIVE_RUNS
        finally:
            config.unregister_active_run(stream_id)
            config.STREAMS.clear()


def test_toolset_route_emits_http_only_after_session_lock_is_released(tmp_path):
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
        sid = session.session_id

        class TrackingLock:
            held = False

            def __enter__(self):
                self.held = True
                return self

            def __exit__(self, exc_type, exc, tb):
                self.held = False
                return False

        lock = TrackingLock()

        def assert_unlocked_j(handler, payload, status=200, **kwargs):
            assert not lock.held
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(payload).encode())
            return True

        with (
            patch("api.routes._get_session_agent_lock", return_value=lock),
            patch("api.routes.j", side_effect=assert_unlocked_j),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["old-toolset"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 200


def test_toolset_change_real_active_run_registry_blocks_until_teardown(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", collections.OrderedDict()),
    ):
        blocked = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        other = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        blocked.save(skip_index=True)
        other.save(skip_index=True)

        blocked_sid = blocked.session_id
        other_sid = other.session_id
        stream_id = "real-active-run"

        config.ACTIVE_RUNS.clear()
        config.STREAMS.clear()
        config.register_active_run(stream_id, session_id=blocked_sid, phase="running")

        try:
            blocked_handler = _DummyHandler({
                "session_id": blocked_sid,
                "toolsets": ["new-toolset"],
            })
            handle_post(blocked_handler, urlparse("/api/session/toolsets"))

            assert blocked_handler.status == 409
            assert json.loads(
                (session_dir / f"{blocked_sid}.json").read_text()
            )["enabled_toolsets"] == ["old-toolset"]

            # A different session is not serialized behind the blocked session's
            # lifecycle row. Use a no-op so no Hermes Agent dependency is needed.
            other_handler = _DummyHandler({
                "session_id": other_sid,
                "toolsets": ["old-toolset"],
            })
            handle_post(other_handler, urlparse("/api/session/toolsets"))
            assert other_handler.status == 200

            config.unregister_active_run(stream_id)

            # After teardown the same session is admitted. Patch only SessionDB;
            # lifecycle predicates remain production-shaped.
            db = Mock()
            with (
                patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
                patch.dict(
                    sys.modules,
                    {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))},
                ),
            ):
                admitted_handler = _DummyHandler({
                    "session_id": blocked_sid,
                    "toolsets": ["new-toolset"],
                })
                handle_post(admitted_handler, urlparse("/api/session/toolsets"))

            assert admitted_handler.status == 200
            assert json.loads(
                (session_dir / f"{blocked_sid}.json").read_text()
            )["enabled_toolsets"] == ["new-toolset"]
        finally:
            config.ACTIVE_RUNS.clear()
            config.STREAMS.clear()


@pytest.mark.parametrize(
    "requested",
    [
        ["b", "a"],
        ["a", "b", "a", "b"],
    ],
)
def test_semantically_identical_toolsets_are_noop_and_keep_pins(tmp_path, requested):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    registry = collections.OrderedDict()
    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", registry),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["a", "b"],
        )
        session.save(skip_index=True)

        with (
            patch.object(session, "save") as save_mock,
            patch(
                "api.routes._active_state_db_path",
                side_effect=AssertionError("semantic no-op must not open SessionDB"),
            ),
        ):
            handler = _DummyHandler({
                "session_id": session.session_id,
                "toolsets": requested,
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 200
        assert handler.payload() == {
            "ok": True,
            "enabled_toolsets": ["a", "b"],
        }
        assert session.enabled_toolsets == ["a", "b"]
        save_mock.assert_not_called()


def test_none_and_empty_toolset_identities_remain_distinct():
    assert routes._session_toolsets_semantic_identity(None) is None
    assert routes._session_toolsets_semantic_identity([]) == frozenset()
    assert (
        routes._session_toolsets_semantic_identity(None)
        != routes._session_toolsets_semantic_identity([])
    )


def test_identical_toolset_selection_is_a_noop(tmp_path):
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
        sid = session.session_id

        with (
            patch.object(session, "save") as save_mock,
            patch(
                "api.routes._active_stream_blocks_chat_start",
                side_effect=AssertionError("no-op must return before active checks"),
            ),
            patch(
                "api.routes._active_state_db_path",
                side_effect=AssertionError("no-op must not open SessionDB"),
            ),
        ):
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["old-toolset"],
            })
            handle_post(handler, urlparse("/api/session/toolsets"))

        assert handler.status == 200
        assert handler.payload() == {
            "ok": True,
            "enabled_toolsets": ["old-toolset"],
        }
        save_mock.assert_not_called()


def test_toolset_update_cold_cache_never_loads_before_session_lock(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)
    registry = collections.OrderedDict()

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", registry),
        patch.object(routes, "SESSION_DIR", session_dir),
        patch.object(routes, "SESSION_INDEX_FILE", index_file),
        patch.object(routes, "SESSIONS", registry),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.messages = [{"role": "user", "content": "keep tombstone"}]
        session.save()
        sid = session.session_id
        sidecar = session_dir / f"{sid}.json"

        # Force the route through the real cold-cache path.
        registry.clear()

        update_waiting_for_lock = threading.Event()
        allow_update_lock = threading.Event()
        real_lock_getter = routes._get_session_agent_lock
        real_session_load = models.Session.load
        load_started = threading.Event()

        def observed_load(target_sid):
            if target_sid == sid:
                load_started.set()
            return real_session_load(target_sid)

        class GateLock:
            def __init__(self, inner):
                self.inner = inner

            def acquire(self, *args, **kwargs):
                return self.inner.acquire(*args, **kwargs)

            def release(self):
                return self.inner.release()

            def __enter__(self):
                update_waiting_for_lock.set()
                assert allow_update_lock.wait(timeout=5)
                self.inner.acquire()
                return self

            def __exit__(self, exc_type, exc, tb):
                self.inner.release()
                return False

        def gated_lock(target_sid):
            return GateLock(real_lock_getter(target_sid))

        db = Mock()
        update_result = {}

        def mutate():
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })
            with (
                patch("api.routes._get_session_agent_lock", side_effect=gated_lock),
                patch.object(models.Session, "load", side_effect=observed_load),
                patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
                patch.dict(
                    sys.modules,
                    {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))},
                ),
            ):
                handle_post(handler, urlparse("/api/session/toolsets"))
            update_result["status"] = handler.status

        safe_delete_patches = (
            patch("api.routes._lookup_cli_session_metadata", return_value={}),
            patch("api.routes._session_is_subagent_view_only", return_value=False),
            patch("api.routes._is_messaging_session_id", return_value=False),
            patch("api.routes._worktree_retained_payload_for_session_id", return_value={}),
            patch("api.routes._publish_session_list_changed", return_value=None),
            patch("api.config._evict_session_agent", return_value=None),
            patch("api.models.delete_cli_session", return_value=True),
            patch("api.upload._session_attachment_dir", return_value=tmp_path / "attachments" / sid),
            patch("api.turn_journal.delete_turn_journal", return_value=None),
            patch("api.run_journal.delete_run_journal", return_value=None),
            patch("api.background_process.forget_bg_task_completion_dedup", return_value=None),
            patch("api.terminal.close_terminal", return_value=None),
        )
        for p in safe_delete_patches:
            p.start()

        try:
            update_thread = threading.Thread(target=mutate)
            update_thread.start()
            assert update_waiting_for_lock.wait(timeout=5)

            # The corrected route has not touched get_session()/Session.load yet.
            # On the buggy pre-lock implementation, a cold lookup would already
            # have started loading the sidecar before reaching this barrier.
            assert not load_started.is_set()
            assert sid not in registry

            # Delete wins the real per-SID lock and completes while update waits.
            delete_handler = _DummyHandler({"session_id": sid})
            handle_post(delete_handler, urlparse("/api/session/delete"))
            assert delete_handler.status == 200
            assert not sidecar.exists()
            assert sid not in registry
            assert sid in models._load_webui_deleted_session_tombstone()

            allow_update_lock.set()
            update_thread.join(timeout=5)

            assert update_result["status"] == 404
            assert not sidecar.exists()
            assert sid not in registry
            assert sid in models._load_webui_deleted_session_tombstone()

            raw_index = json.loads(index_file.read_text()) if index_file.exists() else []
            assert all(row.get("session_id") != sid for row in raw_index)

            registry.clear()
            cold_listing = models.all_sessions()
            assert all(row.get("session_id") != sid for row in cold_listing)

            db.update_system_prompt.assert_not_called()
            db.update_session_tool_names.assert_not_called()
        finally:
            allow_update_lock.set()
            for p in reversed(safe_delete_patches):
                p.stop()


def test_toolset_index_refresh_stays_under_lock_against_same_sid_delete(tmp_path):
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    session_dir.mkdir(parents=True, exist_ok=True)
    registry = collections.OrderedDict()

    with (
        patch.object(models, "SESSION_DIR", session_dir),
        patch.object(models, "SESSION_INDEX_FILE", index_file),
        patch.object(models, "SESSIONS", registry),
        patch.object(routes, "SESSION_DIR", session_dir),
        patch.object(routes, "SESSION_INDEX_FILE", index_file),
        patch.object(routes, "SESSIONS", registry),
    ):
        session = new_session(
            workspace=str(tmp_path),
            enabled_toolsets=["old-toolset"],
        )
        session.save()
        sid = session.session_id
        sidecar = session_dir / f"{sid}.json"

        index_entered = threading.Event()
        allow_index = threading.Event()
        real_write_index = models._write_session_index

        def paused_index_refresh(*args, **kwargs):
            index_entered.set()
            assert allow_index.wait(timeout=5)
            return real_write_index(*args, **kwargs)

        db = Mock()
        toolset_result = {}
        delete_result = {}

        def mutate_toolsets():
            handler = _DummyHandler({
                "session_id": sid,
                "toolsets": ["new-toolset"],
            })
            with (
                patch("api.routes._active_state_db_path", return_value=tmp_path / "state.db"),
                patch("api.routes._write_session_index", side_effect=paused_index_refresh),
                patch.dict(
                    sys.modules,
                    {"hermes_state": SimpleNamespace(SessionDB=Mock(return_value=db))},
                ),
            ):
                handle_post(handler, urlparse("/api/session/toolsets"))
            toolset_result["status"] = handler.status

        class DeleteHandler(_DummyHandler):
            pass

        def delete_session():
            handler = DeleteHandler({"session_id": sid})
            handle_post(handler, urlparse("/api/session/delete"))
            delete_result["status"] = handler.status

        safe_delete_patches = (
            patch("api.routes._lookup_cli_session_metadata", return_value={}),
            patch("api.routes._session_is_subagent_view_only", return_value=False),
            patch("api.routes._is_messaging_session_id", return_value=False),
            patch("api.routes._worktree_retained_payload_for_session_id", return_value={}),
            patch("api.routes._publish_session_list_changed", return_value=None),
            patch("api.config._evict_session_agent", return_value=None),
            patch("api.models.delete_cli_session", return_value=True),
            patch("api.upload._session_attachment_dir", return_value=tmp_path / "attachments" / sid),
            patch("api.turn_journal.delete_turn_journal", return_value=None),
            patch("api.run_journal.delete_run_journal", return_value=None),
            patch("api.background_process.forget_bg_task_completion_dedup", return_value=None),
            patch("api.terminal.close_terminal", return_value=None),
        )

        for p in safe_delete_patches:
            p.start()
        try:
            toolset_thread = threading.Thread(target=mutate_toolsets)
            toolset_thread.start()
            assert index_entered.wait(timeout=5)

            delete_thread = threading.Thread(target=delete_session)
            delete_thread.start()
            delete_thread.join(timeout=0.2)
            assert delete_thread.is_alive(), (
                "same-SID delete must wait while toolset index refresh holds session lock"
            )

            allow_index.set()
            toolset_thread.join(timeout=5)
            delete_thread.join(timeout=5)

            assert toolset_result["status"] == 200
            assert delete_result["status"] == 200
            assert not sidecar.exists()
            assert sid not in registry
            assert sid in models._load_webui_deleted_session_tombstone()

            raw_index = json.loads(index_file.read_text()) if index_file.exists() else []
            assert all(row.get("session_id") != sid for row in raw_index)

            registry.clear()
            cold = models.all_sessions()
            assert all(row.get("session_id") != sid for row in cold)
        finally:
            allow_index.set()
            for p in reversed(safe_delete_patches):
                p.stop()


def test_streaming_snapshots_session_toolsets_under_agent_lock():
    source = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(
        encoding="utf-8"
    )
    run_start = source.index("def _run_agent_streaming(")
    run_end = source.index("\ndef ", run_start + 1)
    run_source = source[run_start:run_end]

    lock_start = run_source.index("with _agent_lock:")
    snapshot_pos = run_source.index(
        '_session_toolsets_override = getattr(s, "enabled_toolsets", None)',
        lock_start,
    )
    lock_end = run_source.index("# TD1: set thread-local env context", lock_start)
    resolve_pos = run_source.index("_toolsets = _resolve_cli_toolsets(_cfg)", lock_end)
    apply_pos = run_source.index(
        "if _session_toolsets_override:",
        resolve_pos,
    )

    assert lock_start < snapshot_pos < lock_end < resolve_pos < apply_pos
    region = run_source[resolve_pos:apply_pos + 500]
    assert "Session.load_metadata_only(session_id)" not in region


@pytest.mark.skipif(
    config._AGENT_DIR is None,
    reason="hermes-agent not found",
)
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
