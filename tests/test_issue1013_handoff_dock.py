"""Regression guards for cross-channel handoff UI and summary generation."""

import json
import time
import sqlite3
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _new_state_db(path: Path) -> sqlite3.Connection:
    """Create a minimal state.db shape for handoff-summary persistence tests."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL
        );
        """
    )
    return conn


def _extract_handoff_marker_payload(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("_handoff_summary_card"):
        return None
    return data


def test_handoff_hint_is_docked_in_composer_flyout_not_transcript():
    """Handoff should use the Terminal-style composer dock, not transcript flow."""
    marker = '<div id="handoffHintContainer"'
    assert marker in INDEX
    msg_inner_idx = INDEX.index('<div class="messages-inner" id="msgInner">')
    composer_flyout_idx = INDEX.index('<div class="composer-flyout">')
    handoff_idx = INDEX.index(marker)
    assert handoff_idx > composer_flyout_idx
    assert not (msg_inner_idx < handoff_idx < composer_flyout_idx)


def test_handoff_dock_reserves_transcript_space_like_terminal_dock():
    assert ".messages.handoff-dock-visible" in STYLE_CSS
    assert ".handoff-hint-container{position:absolute" in STYLE_CSS
    assert "_syncHandoffDockSpace(true)" in SESSIONS_JS
    assert "_syncHandoffDockSpace(false)" in SESSIONS_JS


def test_handoff_dock_width_aligns_with_existing_slide_up_panels():
    assert (
        ".handoff-hint-container{position:absolute;left:0;right:0;bottom:-2px;width:min(calc(100% - 112px),560px);"
        in STYLE_CSS
    )
    assert ".handoff-hint-container{bottom:-2px;width:calc(100% - 28px);}" in STYLE_CSS
    start = STYLE_CSS.find(".handoff-hint-container")
    assert start != -1
    end = STYLE_CSS.find("}", start)
    assert end != -1
    handoff_hint_rule = STYLE_CSS[start : end + 1]
    assert "width:min(calc(100% - 112px),560px)" in handoff_hint_rule
    assert "border-bottom:none;border-radius:13px 13px 0 0" in STYLE_CSS
    assert "padding:7px 12px 9px" in STYLE_CSS
    assert (
        ".handoff-hint-text{min-width:0;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px;font-weight:700;line-height:1.2;"
        in STYLE_CSS
    )
    assert (
        ".handoff-hint-action,.handoff-hint-dismiss{border:none;background:transparent;color:var(--muted);font:inherit;font-size:12px;font-weight:700;line-height:1.2;"
        in STYLE_CSS
    )
    assert (
        ".handoff-hint-dot{width:7px;height:7px;border-radius:999px;background:var(--success);"
        in STYLE_CSS
    )


def test_handoff_summary_fallback_displays_clear_user_note():
    assert "const isFallback=!!state.fallback;" in UI_JS
    assert 'class="handoff-summary-fallback-note"' in UI_JS
    assert (
        "Fallback summary generated from recent turns; no model-based rewrite was used."
        in UI_JS
    )


def test_handoff_delete_clears_local_storage_markers():
    assert "function _clearHandoffStorageForSession(sid) {" in SESSIONS_JS
    assert (
        "_setHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT, null);"
        in SESSIONS_JS
    )
    assert (
        "_setHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT, null);"
        in SESSIONS_JS
    )
    assert "_clearHandoffStorageForSession(sid);" in SESSIONS_JS
    assert "ids.forEach(_clearHandoffStorageForSession);" in SESSIONS_JS


def test_handoff_summary_renders_as_transcript_card_not_dock_card():
    assert "function setHandoffUi" in SESSIONS_JS or "function setHandoffUi" in (
        ROOT / "static" / "ui.js"
    ).read_text(encoding="utf-8")
    ui_js = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_handoffCardsNode" in ui_js
    assert "data-handoff-card" in ui_js
    assert 'data-compression-card="1" data-handoff-card="1"' in ui_js
    assert 'class="tool-card-result handoff-summary-body"' in ui_js
    assert "renderMd(detail)" in ui_js
    assert "_insertCompressionLikeNode(handoffState?_handoffCardsNode" in ui_js
    assert (
        "window._handoffUi&&(!window._handoffUi.sessionId||window._handoffUi.sessionId===sid)"
        in ui_js
    )
    assert "!hasTransientTranscriptUi" in ui_js
    assert "handoff-summary-card" not in SESSIONS_JS
    assert "handoff-summary-card" not in STYLE_CSS


def test_handoff_summary_card_rendering_uses_persisted_messages():
    """Persistent summary markers are parsed from message history and rendered via compression-like cards."""
    assert "_collectHandoffSummaryStates" in UI_JS
    assert "_handoffSummaryStateFromMessage" in UI_JS
    assert "_handoffSummaryPayload" in UI_JS or "_parseHandoffSummaryPayload" in UI_JS
    assert "_insertCompressionLikeNodeByRawIdx" in UI_JS
    assert "_isHandoffSummaryToolPayload" in UI_JS
    assert "_buildHandoffSummaryToolMessage" in SESSIONS_JS


def test_handoff_summary_does_not_call_removed_agent_get_response():
    """Current Hermes Agent exposes run_conversation/private transports, not get_response."""
    handoff_start = ROUTES.index("def _handle_handoff_summary")
    next_handler = ROUTES.index("\ndef _handle_skill_save", handoff_start)
    handoff_body = ROUTES[handoff_start:next_handler]
    assert ".get_response(" not in handoff_body
    assert "_agent_text_completion" in handoff_body
    assert "_fallback_handoff_summary" in handoff_body


def test_handoff_summary_prompt_uses_you_and_你():
    """Summary prompt should use assistant-facing pronouns instead of “user/用户”."""
    handoff_start = ROUTES.index("def _handle_handoff_summary")
    next_handler = ROUTES.index("\ndef _handle_skill_save", handoff_start)
    handoff_body = ROUTES[handoff_start:next_handler]
    prompt_start = handoff_body.index("summary_system_prompt = (")
    prompt_end = handoff_body.index("summary_user_text =", prompt_start)
    prompt_body = handoff_body[prompt_start:prompt_end]

    assert "speak using “you”" in prompt_body
    assert "用“你”" in prompt_body
    assert "the user" not in prompt_body.lower()
    assert "用户" not in prompt_body


def test_generating_handoff_summary_marks_session_as_handled():
    """Summary success uses a max(dismissed/handled) baseline for future checks."""
    generate_start = SESSIONS_JS.index("async function _generateHandoffSummary")
    resolve_start = SESSIONS_JS.index(
        "function _resolveSessionModelForDisplaySoon", generate_start
    )
    generate_body = SESSIONS_JS[generate_start:resolve_start]

    dismiss_start = SESSIONS_JS.index("function _dismissHandoffHint")
    generate_start_after_dismiss = SESSIONS_JS.index(
        "async function _generateHandoffSummary", dismiss_start
    )
    dismiss_body = SESSIONS_JS[dismiss_start:generate_start_after_dismiss]

    assert "_getHandoffSince(sid)" in generate_body
    assert "_setHandoffSummaryHandledAt(sid, Date.now() / 1000)" in generate_body
    assert "_hasMatchingHandoffSummary" not in generate_body
    assert "_setHandoffDismissedAt(" in dismiss_body
    assert "_setHandoffSummaryHandledAt(" not in dismiss_body
    assert "_HANDOFF_SUFFIX_SUMMARY_HANDLED_AT" in SESSIONS_JS
    assert "setHandoffUi({" in generate_body
    assert "phase: 'done'" not in generate_body
    assert "_getHandoffSince(sid)" in SESSIONS_JS
    assert "_HANDOFF_SUFFIX_SUMMARY_HANDLED_AT" in SESSIONS_JS
    assert "_HANDOFF_SUFFIX_DISMISSED_AT" in SESSIONS_JS


def test_handoff_hints_use_max_baseline_since():
    """Handled and dismissed state are coalesced with max() before calling conversation-rounds."""
    check_start = SESSIONS_JS.index("async function _checkAndShowHandoffHint")
    resolve_start = SESSIONS_JS.index("function _showHandoffHint", check_start)
    check_body = SESSIONS_JS[check_start:resolve_start]
    assert "_getHandoffSince(sid)" in check_body
    assert "_getHandoffSummaryHandledAt(sid)" in SESSIONS_JS
    assert "_getHandoffDismissedAt(sid)" in SESSIONS_JS
    assert "Math.max(dismissedAt, summaryHandledAt)" in SESSIONS_JS

    assert "_isHandoffSummaryHandled" not in SESSIONS_JS


def test_no_api_key_handoff_summary_persists_fallback_summary(monkeypatch):
    """No-API-key path should persist fallback summary markers."""
    import api.routes as routes
    import api.config as cfg
    import api.models as models

    # Force API-path validation to focus on fallback behavior only.
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status},
    )
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )

    persisted = []
    monkeypatch.setattr(
        routes,
        "_persist_handoff_summary",
        lambda sid, summary, channel, rounds, fallback=False: (
            persisted.append(
                {
                    "sid": sid,
                    "summary": summary,
                    "channel": channel,
                    "rounds": rounds,
                    "fallback": fallback,
                }
            )
            or {"ok": True}
        ),
    )

    monkeypatch.setattr(
        models,
        "count_conversation_rounds",
        lambda sid, since=None: models.CONVERSATION_ROUND_THRESHOLD,
    )
    monkeypatch.setattr(
        models,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "Need help with setup", "timestamp": 1.0},
            {"role": "assistant", "content": "I'll help you", "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(
        cfg,
        "resolve_model_provider",
        lambda resolved_model=None: ("gpt-test", "openrouter", None),
    )

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "api_key": "",
        "provider": "openrouter",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)

    response = routes._handle_handoff_summary(
        object(), {"session_id": "session-without-api-key"}
    )

    assert response["ok"] is True
    assert response["fallback"] is True
    assert response["summary"].startswith("-")
    assert "You asked:" in response["summary"]
    assert "Recent external-channel activity:" not in response["summary"]
    assert len(persisted) == 1
    assert persisted[0]["sid"] == "session-without-api-key"
    assert persisted[0]["fallback"] is True
    assert persisted[0]["rounds"] == models.CONVERSATION_ROUND_THRESHOLD


def test_exception_handoff_summary_persists_fallback_summary(monkeypatch):
    """Unhandled summary exception should still persist a fallback handoff marker."""
    import api.routes as routes
    import api.config as cfg
    import api.models as models

    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status},
    )
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )

    persisted = []
    monkeypatch.setattr(
        routes,
        "_persist_handoff_summary",
        lambda sid, summary, channel, rounds, fallback=False: (
            persisted.append(
                {
                    "sid": sid,
                    "summary": summary,
                    "channel": channel,
                    "rounds": rounds,
                    "fallback": fallback,
                }
            )
            or {"ok": True}
        ),
    )

    monkeypatch.setattr(
        models,
        "count_conversation_rounds",
        lambda sid, since=None: models.CONVERSATION_ROUND_THRESHOLD,
    )
    monkeypatch.setattr(
        models,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "Could you check this?", "timestamp": 1.0},
            {"role": "assistant", "content": "Sure, I can help", "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(
        cfg,
        "resolve_model_provider",
        lambda resolved_model=None: ("gpt-test", "openrouter", None),
    )

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "api_key": "x",
        "provider": "openrouter",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)

    class _Client:
        class completions:
            @staticmethod
            def create(*args, **kwargs):
                raise RuntimeError("intentional handoff-summary failure")

    class _Chat:
        completions = _Client.completions

    class _OpenAIClient:
        chat = _Chat

    class _FailingAgent:
        api_mode = ""

        def __init__(self, *args, **kwargs):
            self.model = kwargs.get("model")
            self.reasoning_config = None

        def _build_api_kwargs(self, *args, **kwargs):
            return {}

        def _ensure_primary_openai_client(self, reason=None):
            return _OpenAIClient()

        def release_clients(self):
            return None

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FailingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    response = routes._handle_handoff_summary(
        object(), {"session_id": "session-with-exception"}
    )

    assert response["ok"] is True
    assert response["fallback"] is True
    assert response["summary"].startswith("-")
    assert "You asked:" in response["summary"]
    assert "Recent external-channel activity:" not in response["summary"]
    assert "warning" in response
    assert len(persisted) == 1
    assert persisted[0]["sid"] == "session-with-exception"
    assert persisted[0]["fallback"] is True
    assert persisted[0]["rounds"] == models.CONVERSATION_ROUND_THRESHOLD


def test_handoff_summary_retries_once_when_length_limit_reached(monkeypatch):
    """finish_reason='length' should trigger one retry with larger budget."""
    import api.routes as routes
    import api.config as cfg
    import api.models as models

    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status},
    )
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )

    persisted = []
    monkeypatch.setattr(
        routes,
        "_persist_handoff_summary",
        lambda sid, summary, channel, rounds, fallback=False: (
            persisted.append(
                {
                    "sid": sid,
                    "summary": summary,
                    "channel": channel,
                    "rounds": rounds,
                    "fallback": fallback,
                }
            )
            or {"ok": True}
        ),
    )

    monkeypatch.setattr(
        models,
        "count_conversation_rounds",
        lambda sid, since=None: models.CONVERSATION_ROUND_THRESHOLD,
    )
    monkeypatch.setattr(
        models,
        "get_cli_session_messages",
        lambda sid: [
            {
                "role": "user",
                "content": "Can we switch to a different method?",
                "timestamp": 1.0,
            },
            {
                "role": "assistant",
                "content": "Sure, here is the outline.",
                "timestamp": 2.0,
            },
            {"role": "user", "content": "Keep going.", "timestamp": 3.0},
            {
                "role": "assistant",
                "content": "Step 1 is done, step 2 is pending.",
                "timestamp": 4.0,
            },
        ],
    )
    monkeypatch.setattr(
        cfg,
        "resolve_model_provider",
        lambda resolved_model=None: ("gpt-test", "openrouter", None),
    )

    completion_calls = []

    def _choice(content, finish_reason="stop"):
        return types.SimpleNamespace(
            message=types.SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )

    class _Client:
        class completions:
            @staticmethod
            def create(*args, **kwargs):
                max_tokens = kwargs.get("max_tokens") or kwargs.get(
                    "max_completion_tokens"
                )
                completion_calls.append(max_tokens)
                if len(completion_calls) == 1:
                    return types.SimpleNamespace(
                        choices=[
                            _choice(
                                "- You can do step A, B, and C", finish_reason="length"
                            )
                        ]
                    )
                return types.SimpleNamespace(
                    choices=[
                        _choice(
                            "- You should continue with step D.\n- You can then review results.",
                            finish_reason="stop",
                        )
                    ]
                )

    class _Chat:
        completions = _Client.completions

    class _OpenAIClient:
        chat = _Chat

    class _LengthAwareAgent:
        api_mode = ""

        def __init__(self, *args, **kwargs):
            self.model = kwargs.get("model")
            self.reasoning_config = None

        def _build_api_kwargs(self, *args, **kwargs):
            return {}

        def _ensure_primary_openai_client(self, reason=None):
            return _OpenAIClient()

        def release_clients(self):
            return None

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _LengthAwareAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "api_key": "x",
        "provider": "openrouter",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)

    response = routes._handle_handoff_summary(
        object(), {"session_id": "session-length-retry"}
    )

    assert response["ok"] is True
    assert response["fallback"] is False
    assert response["summary"].startswith("- You should continue with step D.")
    assert completion_calls == [700, 1400]
    assert len(persisted) == 1
    assert persisted[0]["fallback"] is False
    assert persisted[0]["sid"] == "session-length-retry"


def test_handoff_summary_falls_back_when_retry_still_incomplete(monkeypatch):
    """Retry may still truncate; fallback should still return deterministic concise bullets."""
    import api.routes as routes
    import api.config as cfg
    import api.models as models

    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status},
    )
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )

    persisted = []
    monkeypatch.setattr(
        routes,
        "_persist_handoff_summary",
        lambda sid, summary, channel, rounds, fallback=False: (
            persisted.append(
                {
                    "sid": sid,
                    "summary": summary,
                    "channel": channel,
                    "rounds": rounds,
                    "fallback": fallback,
                }
            )
            or {"ok": True}
        ),
    )

    monkeypatch.setattr(
        models,
        "count_conversation_rounds",
        lambda sid, since=None: models.CONVERSATION_ROUND_THRESHOLD,
    )
    monkeypatch.setattr(
        models,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "Could you plan next moves?", "timestamp": 1.0},
            {
                "role": "assistant",
                "content": "Let's draft a schedule.",
                "timestamp": 2.0,
            },
            {"role": "user", "content": "Anything else?", "timestamp": 3.0},
            {
                "role": "assistant",
                "content": "Yes, one more check is needed.",
                "timestamp": 4.0,
            },
        ],
    )
    monkeypatch.setattr(
        cfg,
        "resolve_model_provider",
        lambda resolved_model=None: ("gpt-test", "openrouter", None),
    )

    class _Client:
        class completions:
            @staticmethod
            def create(*args, **kwargs):
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content="I can help summarize this but",
                            ),
                            finish_reason="length",
                        )
                    ]
                )

    class _Chat:
        completions = _Client.completions

    class _LengthAwareAgent:
        api_mode = ""

        def __init__(self, *args, **kwargs):
            self.model = kwargs.get("model")
            self.reasoning_config = None

        def _build_api_kwargs(self, *args, **kwargs):
            return {}

        def _ensure_primary_openai_client(self, reason=None):
            return _Chat()

        def release_clients(self):
            return None

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _LengthAwareAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "api_key": "x",
        "provider": "openrouter",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)

    response = routes._handle_handoff_summary(
        object(), {"session_id": "session-length-fallback"}
    )

    assert response["ok"] is True
    assert response["fallback"] is True
    assert response["summary"].startswith("- You asked:")
    assert "Recent external-channel activity:" not in response["summary"]
    assert len(persisted) == 1
    assert persisted[0]["fallback"] is True
    assert persisted[0]["sid"] == "session-length-fallback"


def test_handoff_summary_persistence_targets_both_backends_for_messaging_session(
    tmp_path, monkeypatch
):
    """Messaging sessions should persist handoff summary markers into both local JSON and state.db."""
    import api.routes as routes
    import api.models as models
    import api.profiles as profiles

    sid = "messaging_1013_both_backends_01"
    mock_home = tmp_path / "hermes_home"
    mock_home.mkdir()
    mock_sessions = tmp_path / "sessions"
    mock_sessions.mkdir()

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: mock_home)
    monkeypatch.setattr(models, "SESSION_DIR", mock_sessions)

    conn = _new_state_db(mock_home / "state.db")
    try:
        seed_ts = time.time() - 10
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason) "
            "VALUES (?, 'telegram', 'Messaging Session', 'openai/gpt-5', ?, 0, NULL, NULL, NULL)",
            (sid, seed_ts),
        )
        conn.commit()

        session = models.Session(
            session_id=sid,
            title="Imported Messaging Session",
            workspace=str(tmp_path),
            messages=[{"role": "user", "content": "Need help", "timestamp": 1.0}],
        )
        session.is_cli_session = True
        session.session_source = "messaging"
        session.source_tag = "telegram"
        session.raw_source = "telegram"
        session.source_label = "Telegram"
        session.save(touch_updated_at=False)

        routes._persist_handoff_summary(
            sid, "Please handoff after context", "telegram", 2, False
        )

        saved = models.Session.load(sid)
        assert len(saved.messages) == 2
        marker = saved.messages[-1]
        assert marker.get("name") == "handoff_summary"
        marker_payload = _extract_handoff_marker_payload(marker)
        assert marker_payload is not None
        assert marker_payload.get("session_id") == sid
        assert marker_payload.get("summary") == "Please handoff after context"
        assert marker_payload.get("channel") == "telegram"
        assert marker_payload.get("rounds") == 2

        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY rowid ASC",
            (sid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "tool"
        db_payload = _extract_handoff_marker_payload({"content": rows[0][1]})
        assert db_payload is not None
        assert db_payload.get("session_id") == sid
        assert db_payload.get("summary") == "Please handoff after context"
    finally:
        conn.close()


def test_persisted_handoff_summary_deduplicates_identical_tail_markers(
    tmp_path, monkeypatch
):
    """When the tail already contains the same handoff marker, repeated generation should be idempotent."""
    import api.routes as routes
    import api.models as models
    import api.profiles as profiles

    sid = "messaging_1013_dedupe_tail"
    mock_home = tmp_path / "hermes_home"
    mock_home.mkdir()
    mock_sessions = tmp_path / "sessions"
    mock_sessions.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: mock_home)
    monkeypatch.setattr(models, "SESSION_DIR", mock_sessions)

    conn = _new_state_db(mock_home / "state.db")
    try:
        baseline = time.time()
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason) "
            "VALUES (?, 'telegram', 'Messaging Session', 'openai/gpt-5', ?, 1, NULL, NULL, NULL)",
            (sid, baseline),
        )
        conn.commit()

        marker = routes._build_handoff_summary_tool_message(
            sid, "Repeat me", "telegram", 3, False
        )
        session = models.Session(
            session_id=sid,
            title="Imported Messaging Session",
            workspace=str(tmp_path),
            messages=[
                {"role": "user", "content": "Need help", "timestamp": baseline - 1},
                marker,
            ],
        )
        session.is_cli_session = True
        session.session_source = "messaging"
        session.source_tag = "telegram"
        session.raw_source = "telegram"
        session.source_label = "Telegram"
        session.save(touch_updated_at=False)

        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'tool', ?, ?)",
            (sid, marker["content"], marker["timestamp"]),
        )
        conn.commit()

        routes._persist_handoff_summary(sid, "Repeat me", "telegram", 3, False)

        refreshed = models.Session.load(sid)
        assert len(refreshed.messages) == 2

        rows = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY rowid ASC",
            (sid,),
        ).fetchall()
        assert len(rows) == 1
        assert _extract_handoff_marker_payload({"content": rows[0][0]}) is not None
    finally:
        conn.close()


def test_persist_handoff_summary_falls_back_when_local_session_file_missing(
    tmp_path, monkeypatch
):
    """Messaging session IDs should still persist to state.db when no local WebUI session exists."""
    import api.routes as routes
    import api.profiles as profiles

    sid = "messaging_1013_no_local_file"
    mock_home = tmp_path / "hermes_home"
    mock_home.mkdir()

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: mock_home)
    conn = _new_state_db(mock_home / "state.db")

    # Force messaging classification while keeping the local shell absent.
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: True)
    try:
        routes._persist_handoff_summary(
            sid, "Persist without local shell", "telegram", 1, True
        )
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY rowid ASC",
            (sid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "tool"
        payload = _extract_handoff_marker_payload({"content": rows[0][1]})
        assert payload is not None
        assert payload.get("session_id") == sid
        assert payload.get("fallback") is True
    finally:
        conn.close()
