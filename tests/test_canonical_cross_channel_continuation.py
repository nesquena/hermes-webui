"""Canonical cross-channel continuation: WebUI turn, state.db owner.

A Telegram/Discord/gateway session shown in WebUI must accept a typed
message without claiming a writable sidecar. Claude Code, cron, subagent,
external_agent, unknown, and explicit read_only stay refused.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_chat_start_claim_cli_session import (
    ROUTES_PY,
    _FakePostHandler,
    _make_state_db,
    _response_json,
)


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    state_dir = tmp_path / "webui-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")
    import api.models as _models
    monkeypatch.setattr(_models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    return {
        "db": db,
        "state_dir": state_dir,
        "sessions_dir": sessions_dir,
        "index_path": index_path,
    }

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
MESSAGES_JS = ROOT / "static" / "messages.js"


def _function_body(src: str, name: str) -> str:
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\b", src)
    assert m, f"{name} function not found"
    sig_end = src.find(")", m.end())
    brace_start = src.find("{", sig_end)
    depth = 0
    for idx in range(brace_start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():idx + 1]
    raise AssertionError(f"{name} function body not terminated")


def _stub_chat_start(routes_module, monkeypatch, captured):
    monkeypatch.setattr(routes_module, "_agent_runtime_barrier_response", lambda **_kw: None)
    monkeypatch.setattr(routes_module, "_session_visible_to_active_profile", lambda *a, **k: True)
    monkeypatch.setattr(routes_module, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes_module, "compression_recovery_payload_for_session", lambda _s: None)
    monkeypatch.setattr(
        routes_module,
        "_resolve_chat_workspace_with_recovery",
        lambda _s, _ws: "/tmp/canonical-continuation",
    )
    monkeypatch.setattr(
        routes_module,
        "_read_profile_model_config",
        lambda *_a, **_k: (None, None, {}),
    )
    monkeypatch.setattr(
        routes_module,
        "_resolve_compatible_session_model_state",
        lambda *a, **k: ("test-model", None, False),
    )
    monkeypatch.setattr(
        routes_module,
        "_repair_foreign_session_model_provider",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(routes_module, "webui_gateway_chat_enabled", lambda *_a, **_k: False)

    def _fake_start_run(session, **kwargs):
        captured["session"] = session
        captured["kwargs"] = kwargs
        sidecar = Path(session.path)
        captured["sidecar_exists_during_start"] = sidecar.exists()
        return {
            "stream_id": "stream-canonical",
            "session_id": session.session_id,
            "title": session.title,
        }

    monkeypatch.setattr(routes_module, "_start_run", _fake_start_run)


@pytest.mark.parametrize(
    "source,session_source",
    [
        ("telegram", "messaging"),
        ("discord", "messaging"),
        ("slack", "messaging"),
        ("weixin", "messaging"),
        ("email", "messaging"),
        ("gateway", "other"),
    ],
)
def test_helper_marks_channel_sessions_canonical_continuation(
    routes_module, monkeypatch, isolated_state_db, source, session_source
):
    sid = f"20260821_{source}_continue"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=2,
        title=f"{source} chat", source=source, cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": source,
            "raw_source": source,
            "session_source": session_source,
        },
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(sid)
    assert reason == "canonical_continuation"
    assert sess.session_id == sid
    assert sess.read_only is True
    assert sess.canonical_continuation is True
    sess.save()
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()


@pytest.mark.parametrize(
    "source,session_source",
    [
        ("claude_code", "external_agent"),
        ("cron", "other"),
        ("unknown", "other"),
        ("external_agent", "external_agent"),
        ("subagent", "other"),
    ],
)
def test_helper_still_refuses_unsupported_foreign_sources(
    routes_module, monkeypatch, isolated_state_db, source, session_source
):
    sid = f"20260821_{source}_refuse"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=1,
        title=f"{source} chat", source=source, cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": source,
            "raw_source": source,
            "session_source": session_source,
        },
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(sid)
    assert reason == "not_claimable"
    assert sess.read_only is True
    assert not getattr(sess, "canonical_continuation", False)


def test_explicit_readonly_telegram_stays_not_claimable(
    routes_module, monkeypatch, isolated_state_db
):
    sid = "20260821_telegram_explicit_ro"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=1,
        title="Locked telegram", source="telegram", cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": "telegram",
            "raw_source": "telegram",
            "session_source": "messaging",
            "read_only": True,
        },
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(sid)
    assert reason == "not_claimable"
    assert sess.read_only is True
    assert not getattr(sess, "canonical_continuation", False)


def test_chat_start_continues_telegram_without_sidecar_fork(
    routes_module, monkeypatch, isolated_state_db
):
    sid = "20260821_telegram_chat_start"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=3,
        title="Telegram live", source="telegram", cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": "telegram",
            "raw_source": "telegram",
            "session_source": "messaging",
            "workspace": "/tmp",
            "model": "test-model",
        },
    )
    captured = {}
    _stub_chat_start(routes_module, monkeypatch, captured)
    handler = _FakePostHandler(
        {"session_id": sid, "message": "continue from webui"},
        path="/api/chat/start",
    )
    routes_module._handle_chat_start(
        handler,
        {"session_id": sid, "message": "continue from webui"},
    )
    assert handler.status == 200
    payload = _response_json(handler)
    assert payload["session_id"] == sid
    assert payload["stream_id"] == "stream-canonical"
    assert captured["session"].session_id == sid
    assert captured["session"].canonical_continuation is True
    assert captured["sidecar_exists_during_start"] is False
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()


def test_chat_start_continues_discord_same_as_telegram(
    routes_module, monkeypatch, isolated_state_db
):
    sid = "20260821_discord_chat_start"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=2,
        title="Discord live", source="discord", cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": "discord",
            "raw_source": "discord",
            "session_source": "messaging",
            "workspace": "/tmp",
            "model": "test-model",
        },
    )
    captured = {}
    _stub_chat_start(routes_module, monkeypatch, captured)
    handler = _FakePostHandler(
        {"session_id": sid, "message": "hello from webui"},
        path="/api/chat/start",
    )
    routes_module._handle_chat_start(
        handler,
        {"session_id": sid, "message": "hello from webui"},
    )
    assert handler.status == 200
    assert _response_json(handler)["session_id"] == sid
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()


def test_chat_start_still_403s_claude_code(
    routes_module, monkeypatch, isolated_state_db
):
    sid = "20260821_claude_code_chat_start"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=1,
        title="Claude Code", source="claude_code", cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": sid,
            "source_tag": "claude_code",
            "raw_source": "claude_code",
            "session_source": "external_agent",
        },
    )
    captured = {}
    _stub_chat_start(routes_module, monkeypatch, captured)
    handler = _FakePostHandler(
        {"session_id": sid, "message": "should fail"},
        path="/api/chat/start",
    )
    routes_module._handle_chat_start(
        handler,
        {"session_id": sid, "message": "should fail"},
    )
    assert handler.status == 403
    assert "read-only" in _response_json(handler)["error"].lower()
    assert "session" not in captured
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()


def test_chat_start_keyerror_arm_does_not_save_canonical_sessions():
    src = ROUTES_PY.read_text(encoding="utf-8")
    body = src[src.index("def _handle_chat_start("):]
    body = body[:body.index("\ndef _resolve_chat_workspace_with_recovery")]
    assert 'if reason == "canonical_continuation":' in body
    assert "_activate_canonical_continuation(synth)" in body
    activate_idx = body.index('if reason == "canonical_continuation":')
    save_idx = body.index("synth.save()")
    assert activate_idx < save_idx
    arm = body[activate_idx:save_idx]
    assert "synth.save()" not in arm


def test_import_cli_does_not_materialize_telegram_sidecar(
    routes_module, monkeypatch, isolated_state_db
):
    sid = "20260821_telegram_import"
    _make_state_db(
        isolated_state_db["db"], sid, message_count=2,
        title="Telegram import", source="telegram", cwd="/tmp",
    )
    monkeypatch.setattr(
        routes_module,
        "_resolve_cli_import_metadata",
        lambda _sid, **_kw: {
            "session_id": sid,
            "source_tag": "telegram",
            "raw_source": "telegram",
            "session_source": "messaging",
            "title": "Telegram import",
            "model": "test-model",
        },
    )
    monkeypatch.setattr(
        routes_module,
        "get_cli_session_messages",
        lambda _sid, **_kw: [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *a, **k: True)
    handler = _FakePostHandler({"session_id": sid}, path="/api/session/import_cli")
    routes_module._handle_session_import_cli(handler, {"session_id": sid})
    payload = _response_json(handler)
    assert handler.status == 200
    assert payload["imported"] is False
    assert payload["session"]["canonical_continuation"] is True
    assert payload["session"]["read_only"] is True
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()


def test_session_save_skips_sidecar_for_canonical_continuation(isolated_state_db):
    from api.models import Session

    sid = "20260821_save_skip"
    sess = Session(
        session_id=sid,
        title="Telegram",
        workspace="/tmp",
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        source_tag="telegram",
        raw_source="telegram",
        session_source="messaging",
        canonical_continuation=True,
        read_only=True,
    )
    sess.save()
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()
    compact = sess.compact()
    assert compact["canonical_continuation"] is True
    assert compact["read_only"] is True


def test_frontend_composer_allows_canonical_continuation_only():
    sessions = SESSIONS_JS.read_text(encoding="utf-8")
    messages = MESSAGES_JS.read_text(encoding="utf-8")
    assert "function _sessionAllowsCanonicalContinuation(session)" in sessions
    assert "function _sessionBlocksComposer(session)" in sessions
    blocks = _function_body(sessions, "_sessionBlocksComposer")
    assert "_sessionAllowsCanonicalContinuation(session)" in blocks
    assert "_isReadOnlySession(session)" in blocks
    send = _function_body(messages, "send")
    assert "_sessionBlocksComposer" in send
    branch = _function_body(sessions, "_isBranchableReadOnlySession")
    assert "canonical_continuation" not in branch
    assert "cron" in branch


def test_frontend_hides_handoff_when_canonical_continuation_available():
    sessions = SESSIONS_JS.read_text(encoding="utf-8")
    assert "!_sessionAllowsCanonicalContinuation(S.session)" in sessions
    assert "_checkAndShowHandoffHint(sid)" in sessions


def test_explicit_readonly_messaging_is_not_advertised_as_continuable():
    from api.models import Session, session_uses_canonical_continuation

    sess = Session(
        session_id="20260821_explicit_ro_compact",
        title="Locked telegram",
        workspace="/tmp",
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        source_tag="telegram",
        raw_source="telegram",
        session_source="messaging",
        read_only=True,
        explicit_foreign_readonly=True,
    )
    assert session_uses_canonical_continuation(sess) is False
    assert sess.compact()["canonical_continuation"] is False


def test_streamed_turn_persists_to_state_db_not_sidecar(monkeypatch, isolated_state_db):
    """Real streaming worker writes the turn to state.db under the original id."""
    import queue
    from collections import OrderedDict

    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming
    from api.models import Session, get_state_db_session_messages

    sid = "20260821_telegram_stream_persist"
    db = isolated_state_db["db"]
    sessions_dir = isolated_state_db["sessions_dir"]
    _make_state_db(
        db, sid, message_count=2,
        title="Telegram live", source="telegram", cwd="/tmp",
    )
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: isolated_state_db["state_dir"], raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()

    session = Session(
        session_id=sid,
        title="Telegram live",
        workspace="/tmp",
        model="test-model",
        messages=[
            {"role": "user", "content": "msg 0"},
            {"role": "assistant", "content": "msg 1"},
        ],
        source_tag="telegram",
        raw_source="telegram",
        session_source="messaging",
        canonical_continuation=True,
        read_only=True,
    )
    models.SESSIONS[sid] = session
    assert not (sessions_dir / f"{sid}.json").exists()

    reply = "canonical-stream-reply"

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            import sqlite3
            import time as _time

            user_text = str(kwargs.get("persist_user_message") or "continue from webui")
            now = _time.time()
            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, "user", user_text, now),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, "assistant", reply, now + 0.01),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 2 WHERE id = ?",
                (sid,),
            )
            conn.commit()
            conn.close()
            return {
                "completed": True,
                "final_response": reply,
                "messages": [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": reply},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *a, **k: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *a, **k: [])

    stream_id = "stream-canonical-persist"
    session.active_stream_id = stream_id
    session.pending_user_message = "continue from webui"
    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="continue from webui",
            model="test-model",
            workspace="/tmp",
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    assert not (sessions_dir / f"{sid}.json").exists()
    persisted = get_state_db_session_messages(sid)
    contents = [str(m.get("content") or "") for m in persisted]
    assert "continue from webui" in contents
    assert reply in contents
    assert sid == session.session_id

