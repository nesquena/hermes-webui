"""
Sprint 42 Tests: SessionDB injection into AIAgent for WebUI sessions (PR #356).

Covers:
- streaming.py: SessionDB is initialized inside _run_agent_streaming (import present)
- streaming.py: try/except guards SessionDB init so failures are non-fatal
- streaming.py: session_db= kwarg is passed to AIAgent constructor
- streaming.py: SessionDB init failure prints a WARNING (not silently swallowed)
- streaming.py: SessionDB init is placed before AIAgent construction
"""
import ast
import threading
import pathlib
import re
import queue
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


# ── Shared helpers for sprint-42 additional tests ────────────────────────────

REPO = REPO_ROOT  # alias used by #427 tests
_SESSIONS_JS = REPO_ROOT / 'static' / 'sessions.js'
_STREAMING_PY = REPO_ROOT / 'api' / 'streaming.py'
_MESSAGES_JS = REPO_ROOT / 'static' / 'messages.js'
_UI_JS = REPO_ROOT / 'static' / 'ui.js'

def _read_sessions_js():
    return _SESSIONS_JS.read_text(encoding='utf-8')

# ─────────────────────────────────────────────────────────────────────────────

class TestSessionDBInjection(unittest.TestCase):
    """Verify SessionDB is initialized and passed to AIAgent in streaming.py."""

    def test_hermes_state_import_present(self):
        """SessionDB must be imported from hermes_state inside _run_agent_streaming."""
        self.assertIn(
            "from hermes_state import SessionDB",
            STREAMING_PY,
            "SessionDB import missing from streaming.py (PR #356)",
        )

    def test_session_db_kwarg_passed_to_agent(self):
        """session_db= must be passed to the AIAgent constructor call."""
        self.assertIn(
            "session_db=_session_db",
            STREAMING_PY,
            "session_db kwarg not passed to AIAgent (PR #356)",
        )

    def test_sessiondb_init_in_try_except(self):
        """SessionDB init must be wrapped in try/except for non-fatal failure handling."""
        # Check that SessionDB init is wrapped in the helper used by streaming.
        helper_start = STREAMING_PY.find("def _build_session_db_for_stream")
        helper_end = STREAMING_PY.find("\n\ndef _attempt_credential_self_heal", helper_start)
        self.assertGreater(helper_start, -1, "session DB helper missing in streaming.py")
        helper_src = STREAMING_PY[helper_start:helper_end]
        pattern = (
            r"def _build_session_db_for_stream"
            r"[\s\S]*?try:\s*\n[\s\S]*?from hermes_state import SessionDB[\s\S]*?return SessionDB\(db_path=state_db_path\)[\s\S]*?except Exception as _db_err:"
        )
        self.assertRegex(
            helper_src,
            pattern,
            "SessionDB init helper must use try/except for non-fatal error handling",
        )

    def test_sessiondb_retry_only_targets_transient_sqlite_errors(self):
        """Permanent constructor errors must leave the retry loop immediately."""
        helper_start = STREAMING_PY.find("def _build_session_db_for_stream")
        helper_end = STREAMING_PY.find("\n\ndef _attempt_credential_self_heal", helper_start)
        helper_src = STREAMING_PY[helper_start:helper_end]
        self.assertIn("except sqlite3.OperationalError as _db_err", helper_src)
        self.assertIn('"locked" in _db_err_text or "busy" in _db_err_text', helper_src)
        self.assertIn("raise _last_error or RuntimeError", helper_src)

    def test_sessiondb_failure_logs_warning(self):
        """A failure initializing SessionDB must print a WARNING (not silently drop the error)."""
        self.assertIn(
            "WARNING: SessionDB init failed",
            STREAMING_PY,
            "SessionDB init failure must log a WARNING message (PR #356)",
        )

    def test_session_db_initialized_before_agent_construction(self):
        """SessionDB initialization must appear before the AIAgent(...) constructor call."""
        db_pos = STREAMING_PY.find("from hermes_state import SessionDB")
        agent_pos = STREAMING_PY.find("session_db=_session_db")
        self.assertGreater(
            agent_pos,
            db_pos,
            "SessionDB init must appear before AIAgent construction (PR #356)",
        )

    def test_session_db_default_is_none(self):
        """SessionDB should now be initialized through the helper call."""
        pattern = "_state_db_path = (Path(_profile_home) / \"state.db\") if _profile_home else None"
        helper_pattern = "_session_db = _build_session_db_for_stream(_state_db_path)"
        self.assertIn(
            pattern,
            STREAMING_PY,
            "_state_db_path should be resolved from profile home in streaming.py",
        )
        self.assertIn(
            helper_pattern,
            STREAMING_PY,
            "_session_db should be initialized via _build_session_db_for_stream in streaming.py",
        )


class TestRuntimeRouteInjection(unittest.TestCase):
    """Verify WebUI forwards the resolved runtime route into AIAgent."""

    def test_runtime_provider_keys_are_forwarded_to_agent(self):
        """WebUI must pass the runtime route fields that CLI already uses.

        Since issue #772 these are passed defensively via inspect-guarded kwargs
        so the WebUI degrades gracefully against older hermes-agent builds.
        """
        for snippet in (
            "_agent_kwargs['api_mode'] = _rt.get('api_mode')",
            "_agent_kwargs['acp_command'] = _rt.get('command')",
            "_agent_kwargs['acp_args'] = _rt.get('args')",
            "_agent_kwargs['credential_pool'] = _rt.get('credential_pool')",
        ):
            self.assertIn(
                snippet,
                STREAMING_PY,
                f"Missing defensive runtime route forwarding in streaming.py: {snippet}",
            )

    def test_runtime_route_is_forwarded_from_resolver_into_agent_init(self):
        """The resolved ACP route should be passed through to AIAgent kwargs."""
        import api.streaming as streaming

        captured = {}
        fake_session_db = object()
        resolve_runtime_provider = mock.Mock(
            return_value={
                "provider": "openai-codex",
                "base_url": "https://api.openai.com/v1",
                "api_key": "rt-key",
                "api_mode": "codex_responses",
                "command": "codex",
                "args": ["exec", "--json"],
                "credential_pool": "openai-codex",
            }
        )

        class FakeSession:
            def __init__(self):
                self.session_id = "sess-runtime-route"
                self.title = "Existing title"
                self.workspace = "/tmp"
                self.model = "gpt-5.4"
                self.messages = []
                self.personality = None
                self.input_tokens = 0
                self.output_tokens = 0
                self.estimated_cost = None
                self.tool_calls = []
                self.active_stream_id = None
                self.pending_user_message = None
                self.pending_attachments = []
                self.pending_started_at = None

            def save(self, touch_updated_at=True):
                self._saved = True

            def compact(self):
                return {
                    "session_id": self.session_id,
                    "title": self.title,
                    "workspace": self.workspace,
                    "model": self.model,
                    "created_at": 0,
                    "updated_at": 0,
                    "pinned": False,
                    "archived": False,
                    "project_id": None,
                    "profile": None,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "estimated_cost": self.estimated_cost,
                    "personality": self.personality,
                }

        class CapturingAgent:
            def __init__(self, model=None, provider=None, base_url=None, api_key=None,
                         api_mode=None, acp_command=None, acp_args=None,
                         credential_pool=None, platform=None, quiet_mode=False,
                         enabled_toolsets=None, fallback_model=None, session_id=None,
                         session_db=None, stream_delta_callback=None,
                         reasoning_callback=None, tool_progress_callback=None,
                         clarify_callback=None, **kwargs):
                captured["init_kwargs"] = dict(
                    model=model, provider=provider, base_url=base_url,
                    api_key=api_key, api_mode=api_mode, acp_command=acp_command,
                    acp_args=acp_args, credential_pool=credential_pool,
                    platform=platform, quiet_mode=quiet_mode,
                    enabled_toolsets=enabled_toolsets, fallback_model=fallback_model,
                    session_id=session_id, session_db=session_db,
                    stream_delta_callback=stream_delta_callback,
                    reasoning_callback=reasoning_callback,
                    tool_progress_callback=tool_progress_callback,
                    clarify_callback=clarify_callback,
                )
                self.session_id = session_id
                self.context_compressor = None
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self.session_estimated_cost_usd = None
                self.reasoning_config = None
                self.ephemeral_system_prompt = None
                self._last_error = None

            def run_conversation(self, **kwargs):
                captured["run_kwargs"] = kwargs
                return {
                    "messages": [
                        {"role": "user", "content": kwargs["persist_user_message"]},
                        {"role": "assistant", "content": "ok"},
                    ]
                }

            def interrupt(self, _message):
                captured["interrupted"] = True

        fake_session = FakeSession()
        fake_stream_id = "stream-runtime-route"
        fake_session.active_stream_id = fake_stream_id
        fake_queue = queue.Queue()
        fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
        fake_runtime_module.resolve_runtime_provider = resolve_runtime_provider
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_hermes_cli.runtime_provider = fake_runtime_module
        fake_hermes_state = types.ModuleType("hermes_state")
        fake_hermes_state.SessionDB = mock.Mock(return_value=fake_session_db)

        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=CapturingAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-5.4", "openai-codex", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch.dict(
                 sys.modules,
                 {
                     "hermes_cli": fake_hermes_cli,
                     "hermes_cli.runtime_provider": fake_runtime_module,
                     "hermes_state": fake_hermes_state,
                 },
             ):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="hello from webui",
                model="gpt-5.4",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )

        # #4022: the resolver is now called with the target model too so per-model
        # base_url normalization (e.g. OpenCode-Go /v1 stripping) is applied.
        resolve_runtime_provider.assert_called_once_with(
            requested="openai-codex", target_model="gpt-5.4"
        )
        init_kwargs = captured["init_kwargs"]
        self.assertEqual(init_kwargs["api_mode"], "codex_responses")
        self.assertEqual(init_kwargs["acp_command"], "codex")
        self.assertEqual(init_kwargs["acp_args"], ["exec", "--json"])
        self.assertEqual(init_kwargs["credential_pool"], "openai-codex")
        self.assertEqual(init_kwargs["api_key"], "rt-key")
        self.assertIs(init_kwargs["session_db"], fake_session_db)

    def test_runtime_provider_forwards_interim_assistant_callback(self):
        """WebUI must pass interim_assistant_callback to AIAgent and emit SSE events."""
        import api.streaming as streaming

        captured = {}

        class CapturingAgent:
            def __init__(
                self,
                model=None,
                provider=None,
                base_url=None,
                api_key=None,
                platform=None,
                quiet_mode=False,
                enabled_toolsets=None,
                fallback_model=None,
                session_id=None,
                session_db=None,
                stream_delta_callback=None,
                reasoning_callback=None,
                tool_progress_callback=None,
                interim_assistant_callback=None,
                clarify_callback=None,
                **kwargs,
            ):
                captured["init_kwargs"] = dict(
                    model=model, provider=provider, base_url=base_url, api_key=api_key,
                    platform=platform, quiet_mode=quiet_mode,
                    enabled_toolsets=enabled_toolsets, fallback_model=fallback_model,
                    session_id=session_id, session_db=session_db,
                    stream_delta_callback=stream_delta_callback,
                    reasoning_callback=reasoning_callback,
                    tool_progress_callback=tool_progress_callback,
                    interim_assistant_callback=interim_assistant_callback,
                    clarify_callback=clarify_callback,
                )
                self.session_id = session_id
                self.context_compressor = None
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self.session_estimated_cost_usd = None
                self.reasoning_config = None
                self.ephemeral_system_prompt = None
                self._last_error = None
                self.interim_assistant_callback = interim_assistant_callback
                captured["agent"] = self

            def run_conversation(self, **kwargs):
                if self.interim_assistant_callback:
                    self.interim_assistant_callback("Inspecting repo structure.", already_streamed=False)
                return {
                    "messages": [
                        {"role": "user", "content": kwargs.get("persist_user_message", "")},
                        {"role": "assistant", "content": "ok"},
                    ]
                }

            def interrupt(self, _message):
                captured["interrupted"] = True

        class FakeSession:
            session_id = "sess-interim-test"
            title = "Test"
            workspace = "/tmp"
            model = "gpt-4o"
            messages = []
            personality = None
            input_tokens = 0
            output_tokens = 0
            estimated_cost = None
            tool_calls = []
            active_stream_id = None
            pending_user_message = None
            pending_attachments = []
            pending_started_at = None

            def save(self, touch_updated_at=True, skip_index=True):
                pass

            def compact(self):
                return {
                    "session_id": self.session_id, "title": self.title,
                    "workspace": self.workspace, "model": self.model,
                    "created_at": 0, "updated_at": 0, "pinned": False,
                    "archived": False, "project_id": None, "profile": None,
                    "input_tokens": 0, "output_tokens": 0,
                    "estimated_cost": None, "personality": None,
                }

            @property
            def path(self):
                return "/tmp/fake.json"

        fake_stream_id = "stream-interim-callback"
        fake_queue = queue.Queue()
        fake_rt_module = types.ModuleType("hermes_cli.runtime_provider")
        fake_rt_module.resolve_runtime_provider = mock.Mock(return_value={
            "provider": "openai-codex",
            "base_url": "https://api.openai.com/v1",
            "api_key": "rt-key",
            "api_mode": "codex_responses",
            "command": "codex",
            "args": ["exec", "--json"],
            "credential_pool": object(),
        })
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_hermes_cli.runtime_provider = fake_rt_module
        fake_hermes_state = types.ModuleType("hermes_state")
        fake_hermes_state.SessionDB = mock.Mock(return_value=object())

        fake_session = FakeSession()
        fake_session.active_stream_id = fake_stream_id

        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=CapturingAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-4o", "openai-codex", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch.dict(sys.modules, {
                 "hermes_cli": fake_hermes_cli,
                 "hermes_cli.runtime_provider": fake_rt_module,
                 "hermes_state": fake_hermes_state,
             }):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id="sess-interim-test",
                msg_text="hello",
                model="gpt-4o",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )

        init_kwargs = captured["init_kwargs"]
        self.assertIsNotNone(init_kwargs["interim_assistant_callback"])
        self.assertTrue(callable(init_kwargs["interim_assistant_callback"]))
        self.assertIn("WebUI progress guidance", captured["agent"].ephemeral_system_prompt)
        self.assertIn("Match the normal Hermes messaging style", captured["agent"].ephemeral_system_prompt)
        self.assertIn(
            "do not let long tool-running WebUI turns appear silent",
            captured["agent"].ephemeral_system_prompt,
        )
        self.assertIn(
            "emit brief user-visible progress updates as normal assistant content",
            captured["agent"].ephemeral_system_prompt,
        )
        self.assertIn(
            "Before the first tool batch in a long task",
            captured["agent"].ephemeral_system_prompt,
        )
        self.assertIn(
            "Do not run many independent tool batches back-to-back without visible assistant text between them",
            captured["agent"].ephemeral_system_prompt,
        )
        self.assertIn(
            "Do not keep progress only in reasoning, thinking, or tool-result channels",
            captured["agent"].ephemeral_system_prompt,
        )
        self.assertNotIn(
            "you may provide brief user-visible progress updates",
            captured["agent"].ephemeral_system_prompt,
        )

        interim_events = []
        while not fake_queue.empty():
            try:
                item = fake_queue.get_nowait()
                if isinstance(item, tuple) and len(item) >= 2:
                    interim_events.append((item[0], item[1]))
                else:
                    interim_events.append(item)
            except queue.Empty:
                break
        self.assertTrue(
            any(event == "interim_assistant" for event, _ in interim_events),
            "interim_assistant callback should emit interim_assistant SSE events",
        )
        self.assertTrue(
            any(
                event == "interim_assistant" and event_data.get("text") == "Inspecting repo structure."
                for event, event_data in interim_events
            ),
            "interim_assistant event should carry the assistant commentary text"
        )

    def test_clarify_callback_passes_configured_timeout_seconds(self):
        """clarify prompt data should use clarify.timeout from config when present."""
        import api.streaming as streaming

        captured = {}
        submit_payloads = []

        class FakeEntry:
            def __init__(self, value):
                self.result = value
                self.event = threading.Event()
                self.event.set()

        def fake_submit_pending(_sid, payload):
            submit_payloads.append(payload)
            return FakeEntry("selected")

        class CapturingAgent:
            def __init__(self, model=None, provider=None, base_url=None, api_key=None,
                         platform=None, quiet_mode=False, enabled_toolsets=None,
                         fallback_model=None, session_id=None, session_db=None,
                         stream_delta_callback=None, reasoning_callback=None,
                         tool_progress_callback=None, clarify_callback=None, **kwargs):
                self.clarify_callback = clarify_callback
                self.session_id = session_id
                captured["init_kwargs"] = {
                    "clarify_callback": clarify_callback,
                }

            def run_conversation(self, **kwargs):
                if self.clarify_callback:
                    captured["clarify_result"] = self.clarify_callback(
                        "Need user confirmation",
                        ["first", "second"],
                    )
                return {
                    "messages": [
                        {"role": "user", "content": kwargs.get("persist_user_message", "")},
                        {"role": "assistant", "content": "ok"},
                    ]
                }

            def interrupt(self, _message):
                captured["interrupted"] = True

        class FakeSession:
            session_id = "sess-clarify-timeout"
            title = "clarify-timeout test"
            workspace = "/tmp"
            model = "gpt-5.4"
            messages = []
            personality = None
            input_tokens = 0
            output_tokens = 0
            estimated_cost = None
            tool_calls = []
            active_stream_id = None
            pending_user_message = None
            pending_attachments = []
            pending_started_at = None

            def save(self, touch_updated_at=True, **_kwargs):
                pass

            def compact(self):
                return {
                    "session_id": self.session_id,
                    "title": self.title,
                    "workspace": self.workspace,
                    "model": self.model,
                    "created_at": 0,
                    "updated_at": 0,
                    "pinned": False,
                    "archived": False,
                    "project_id": None,
                    "profile": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost": None,
                    "personality": None,
                }

            @property
            def path(self):
                return "/tmp/fake.json"

        fake_stream_id = "stream-clarify-timeout"
        fake_queue = queue.Queue()
        fake_rt_module = types.ModuleType("hermes_cli.runtime_provider")
        fake_rt_module.resolve_runtime_provider = mock.Mock(return_value={
            "provider": "openai-codex",
            "base_url": "https://api.openai.com/v1",
            "api_key": "rt-key",
            "api_mode": "codex_responses",
            "command": "codex",
            "args": ["exec", "--json"],
            "credential_pool": object(),
        })
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_hermes_cli.runtime_provider = fake_rt_module
        fake_hermes_state = types.ModuleType("hermes_state")
        fake_hermes_state.SessionDB = mock.Mock(return_value=object())

        fake_session = FakeSession()
        fake_session.active_stream_id = fake_stream_id

        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=CapturingAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-5.4", "openai-codex", None)), \
             mock.patch.object(streaming, "get_config", return_value={"clarify": {"timeout": 300}}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch("api.clarify.submit_pending", side_effect=fake_submit_pending), \
             mock.patch.dict(sys.modules, {
                "hermes_cli": fake_hermes_cli,
                "hermes_cli.runtime_provider": fake_rt_module,
                "hermes_state": fake_hermes_state,
             }):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id="sess-clarify-timeout",
                msg_text="please run task",
                model="gpt-5.4",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )

        self.assertEqual(captured["clarify_result"], "selected")
        self.assertEqual(len(submit_payloads), 1)
        self.assertEqual(submit_payloads[0]["timeout_seconds"], 300)


class TestSessionDBAST(unittest.TestCase):
    """AST-level checks: verify the try/except is not inside _ENV_LOCK (deadlock guard)."""

    def setUp(self):
        self.tree = ast.parse(STREAMING_PY)

    def test_sessiondb_try_not_inside_env_lock(self):
        """The try block that wraps SessionDB init must NOT be inside a 'with _ENV_LOCK:' block.

        Putting a try/except inside _ENV_LOCK is the deadlock pattern caught by test_sprint34.
        The SessionDB try/except is outside the lock scope, which is correct.
        """
        # Find all 'with _ENV_LOCK:' nodes; check none of their bodies contain
        # a Try node that also contains 'from hermes_state import SessionDB'
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            names = [getattr(item.context_expr, "id", "") for item in node.items]
            if "_ENV_LOCK" not in names:
                continue
            # Walk the with-body for Try nodes
            for stmt in node.body:
                if isinstance(stmt, ast.Try):
                    # Check if this try imports hermes_state
                    src = ast.unparse(stmt)
                    self.assertNotIn(
                        "hermes_state",
                        src,
                        "SessionDB try/except must NOT be inside _ENV_LOCK body (deadlock risk)",
                    )


class TestModelCustomInput(unittest.TestCase):
    """Tests for issue #444 — custom model ID input in model dropdown."""

    STATIC = pathlib.Path(__file__).parent.parent / 'static'

    def _read(self, filename):
        path = self.STATIC / filename
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def _renderModelDropdown_body(self):
        src = self._read('ui.js')
        start = src.find('function renderModelDropdown()')
        end = src.find('\nasync function selectModelFromDropdown', start)
        return src[start:end]

    def test_model_custom_input_in_dropdown(self):
        body = self._renderModelDropdown_body()
        self.assertIn('model-custom-input', body,
                      'model-custom-input class must be in renderModelDropdown')

    def test_model_custom_enter_handler(self):
        body = self._renderModelDropdown_body()
        self.assertIn('_applyCustom', body,
                      '_applyCustom function must be defined in renderModelDropdown')

    def test_model_custom_css_defined(self):
        css = self._read('style.css')
        self.assertIn('.model-custom-row', css,
                      '.model-custom-row must be defined in style.css')
        self.assertIn('.model-custom-input', css,
                      '.model-custom-input must be defined in style.css')

    def test_model_custom_i18n_keys(self):
        i18n = self._read('i18n.js')
        # Find en locale block (appears first before es)
        en_block_start = i18n.find("'en'")
        es_block_start = i18n.find("'es'")
        en_block = i18n[en_block_start:es_block_start]
        self.assertIn('model_custom_label', en_block,
                      'model_custom_label must be in en locale')
        self.assertIn('model_custom_placeholder', en_block,
                      'model_custom_placeholder must be in en locale')


# ── Sprint 42 additional tests: context indicator (#437) ─────────────────
def test_context_indicator_uses_pick_helper():
    """The _pick helper must be present in sessions.js to prefer latest over stale values."""
    content = _read_sessions_js()
    assert '_pick' in content, "_pick helper not found in static/sessions.js"


def test_context_indicator_old_pattern_removed():
    """The old || pattern that preferred stale session data must be gone."""
    content = _read_sessions_js()
    assert '_s.input_tokens||u.input_tokens' not in content, \
        "Old stale-data-first pattern '_s.input_tokens||u.input_tokens' still present in static/sessions.js"


def test_context_indicator_all_six_fields():
    """All six token/cost fields must appear in the _syncCtxIndicator call."""
    content = _read_sessions_js()
    fields = [
        'input_tokens',
        'output_tokens',
        'estimated_cost',
        'context_length',
        'last_prompt_tokens',
        'threshold_tokens',
    ]
    for field in fields:
        assert field in content, \
            f"Field '{field}' not found in static/sessions.js _syncCtxIndicator call"


# ── Sprint 42 additional tests: system prompt title (#441) ──────────────
def test_system_prompt_title_guard_exists():
    """The guard that detects [SYSTEM: prefixes must be present in sessions.js."""
    content = _read_sessions_js()
    assert '[SYSTEM:' in content, \
        "sessions.js must contain the [SYSTEM: guard to intercept system-prompt titles"
    # Make sure it appears in an if-condition context, not just a comment
    assert "cleanTitle.startsWith('[SYSTEM:')" in content, \
        "sessions.js must have: cleanTitle.startsWith('[SYSTEM:') guard expression"


def test_cleanTitle_is_let_not_const():
    """cleanTitle must be declared with let (not const) to allow reassignment in the guard."""
    content = _read_sessions_js()
    assert 'let cleanTitle' in content, \
        "cleanTitle must be declared with 'let' (not 'const') to allow reassignment"
    # Make sure the old const form is gone in this context
    # (check the specific assignment line pattern)
    assert "const cleanTitle=tags.length" not in content, \
        "Old 'const cleanTitle=tags.length...' must be replaced by 'let cleanTitle=...'"


# ── Sprint 42 additional tests: thinking panel persistence (#427) ────────
def test_streaming_persists_reasoning_in_session():
    """streaming.py must accumulate reasoning and patch assistant messages."""
    src = (REPO / 'api' / 'streaming.py').read_text(encoding="utf-8")

    # #3587: per-message reasoning segments replaced the flat _reasoning_text accumulator
    assert "_reasoning_segments" in src, \
        "_reasoning_segments dict not found in streaming.py"

    # on_reasoning must accumulate non-echo reasoning into segments
    assert '_reasoning_segments[_current_reasoning_idx]' in src or '_reasoning_segments.get(_current_reasoning_idx' in src, \
        "on_reasoning callback does not accumulate into per-message _reasoning_segments"
    assert '_is_visible_output_echo(reasoning_delta)' in src, \
        "on_reasoning callback should suppress reasoning deltas that only echo visible streamed output"

    # Persistence block must exist before raw_session is built
    assert "Persist reasoning trace in the session so it survives reload" in src, \
        "Reasoning persistence comment not found in streaming.py"

    # #3455: reasoning is now persisted via the think-split path — either the
    # merged reasoning (inline <think> + on_reasoning stream) or the existing
    # _reasoning_text when content has no leading block. Both set _rm['reasoning'].
    assert "_rm['reasoning'] = _merged_reasoning" in src, \
        "Code to set the last assistant message's reasoning (merged think-split) not found"
    assert "_split_thinking_from_content(" in src, \
        "server-side think-split must run before save (#3455)"
    assert "_rm['reasoning'] = _existing_reasoning" in src, \
        "the no-think-block branch must still persist _reasoning_text into the assistant message"

    # Persistence block must come BEFORE the settled raw_session payload is built
    persist_idx = src.index("Persist reasoning trace in the session")
    raw_session_idx = src.index("raw_session = _session_payload_with_full_messages")
    assert persist_idx < raw_session_idx, \
        "Reasoning persistence block must appear before raw_session assignment"


def test_done_handler_patches_reasoning_field():
    """messages.js done SSE handler must patch reasoningText onto the last assistant message."""
    src = (REPO / 'static' / 'messages.js').read_text(encoding="utf-8")

    # The persistence comment must be present inside the done handler
    assert "Persist reasoning trace for Worklog Thinking Cards" in src, \
        "Reasoning persistence comment not found in messages.js done handler"

    # The guard and assignment must be present
    assert "if(reasoningText&&lastAsst&&!lastAsst.reasoning)" in src, \
        "reasoningText guard not found in messages.js"

    assert "lastAsst.reasoning=reasoningText" in src, \
        "lastAsst.reasoning assignment not found in messages.js"

    # Verify the patch is inside the done handler (after 'source.addEventListener' for done)
    done_handler_idx = src.index("source.addEventListener('done'")
    persist_idx = src.index("Persist reasoning trace for Worklog Thinking Cards")
    assert done_handler_idx < persist_idx, \
        "Reasoning persistence patch must be inside the done SSE handler"

    # The guard must also check !lastAsst.reasoning to avoid overwriting server value
    assert "!lastAsst.reasoning" in src, \
        "Guard '!lastAsst.reasoning' missing — would overwrite server-persisted reasoning"


def test_rendermessages_keeps_reasoning_metadata_out_of_worklog_display():
    """ui.js renderMessages must not promote provider reasoning metadata into Worklog prose."""
    src = (REPO / 'static' / 'ui.js').read_text(encoding="utf-8")

    sig_fn = src.split("function _messageHasReasoningPayload(m)", 1)[1].split("function", 1)[0]
    assert 'm.reasoning' in sig_fn, \
        "m.reasoning should remain part of metadata/cache signature handling"

    # Legacy thinking-card helpers may still exist for explicit debug surfaces.
    assert 'thinking-card' in src, \
        "thinking-card CSS class not found in ui.js"

    extraction = src.split("let thinkingText='';", 1)[1].split("const isUser=m.role==='user';", 1)[0]
    assert 'm.reasoning' not in extraction
    assert 'm.reasoning_content' not in extraction


def test_streaming_restores_prior_reasoning_metadata_after_followup():
    """Previous-turn thinking must survive later turns.

    The provider-facing history strips WebUI-only `reasoning` fields, so the
    streaming path must merge that metadata back onto the returned message
    history before saving the session, including reinserting dropped
    reasoning-only assistant segments.
    """
    src = (REPO / 'api' / 'streaming.py').read_text(encoding="utf-8")
    assert "def _restore_reasoning_metadata(" in src, \
        "streaming.py must define a helper to restore prior reasoning metadata"
    assert "_next_context_messages" in src and "s.context_messages" in src, \
        "streaming.py must restore prior reasoning metadata into model context"
    assert "s.messages = _merge_display_messages_after_agent_result(" in src, \
        "streaming.py must merge restored result messages into the visible transcript"
    assert "updated_messages.insert(safe_pos, copy.deepcopy(prev_msg))" in src, \
        "streaming.py must reinsert dropped reasoning-only assistant messages"


def test_routes_restores_prior_reasoning_metadata_after_followup():
    """The non-streaming route path must preserve prior reasoning metadata too."""
    src = (REPO / 'api' / 'routes.py').read_text(encoding="utf-8")
    assert "_restore_reasoning_metadata" in src, \
        "routes.py must import reasoning metadata restoration helper"
    assert "_next_context_messages" in src and "s.context_messages" in src, \
        "routes.py must restore prior reasoning metadata into model context"
    assert 's.messages = _merge_display_messages_after_agent_result(' in src, \
        "routes.py must merge restored result messages into the visible transcript"


class TestCredentialPoolBackwardCompat(unittest.TestCase):
    """Verify credential_pool and other newer kwargs are skipped gracefully
    when running against an older hermes-agent that lacks them (issue #772)."""

    def test_older_agent_without_credential_pool_does_not_crash(self):
        """WebUI must not crash with TypeError when AIAgent lacks credential_pool."""
        import api.streaming as streaming

        captured = {}

        class OlderAgent:
            """Simulates a hermes-agent build that predates credential_pool."""
            def __init__(self, model=None, provider=None, base_url=None, api_key=None,
                         platform=None, quiet_mode=False, enabled_toolsets=None,
                         fallback_model=None, session_id=None, session_db=None,
                         stream_delta_callback=None, reasoning_callback=None,
                         tool_progress_callback=None, clarify_callback=None):
                # No api_mode / acp_command / acp_args / credential_pool params
                captured["init_kwargs"] = {"session_id": session_id, "model": model}
                self.session_id = session_id
                self.context_compressor = None
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self.session_estimated_cost_usd = None
                self.reasoning_config = None
                self.ephemeral_system_prompt = None
                self._last_error = None

            def run_conversation(self, **kwargs):
                return {
                    "messages": [
                        {"role": "user", "content": kwargs.get("persist_user_message", "")},
                        {"role": "assistant", "content": "ok"},
                    ]
                }

            def interrupt(self, _message):
                pass

        class FakeSession:
            session_id = "sess-compat-test"
            title = "Test"
            workspace = "/tmp"
            model = "gpt-4o"
            messages = []
            personality = None
            input_tokens = 0
            output_tokens = 0
            estimated_cost = None
            tool_calls = []
            active_stream_id = None
            pending_user_message = None
            pending_attachments = []
            pending_started_at = None

            def save(self, touch_updated_at=True):
                pass

            def compact(self):
                return {
                    "session_id": self.session_id, "title": self.title,
                    "workspace": self.workspace, "model": self.model,
                    "created_at": 0, "updated_at": 0, "pinned": False,
                    "archived": False, "project_id": None, "profile": None,
                    "input_tokens": 0, "output_tokens": 0,
                    "estimated_cost": None, "personality": None,
                }

        fake_stream_id = "stream-compat-test"
        fake_queue = queue.Queue()
        fake_rt_module = types.ModuleType("hermes_cli.runtime_provider")
        fake_rt_module.resolve_runtime_provider = mock.Mock(return_value={
            "provider": "openai", "base_url": None, "api_key": "sk-test",
            "api_mode": "chat_completions", "command": None, "args": [],
            "credential_pool": object(),
        })
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_hermes_cli.runtime_provider = fake_rt_module
        fake_hermes_state = types.ModuleType("hermes_state")
        fake_hermes_state.SessionDB = mock.Mock(return_value=None)

        fake_session = FakeSession()
        fake_session.active_stream_id = fake_stream_id

        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=OlderAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-4o", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch.dict(sys.modules, {
                 "hermes_cli": fake_hermes_cli,
                 "hermes_cli.runtime_provider": fake_rt_module,
                 "hermes_state": fake_hermes_state,
             }):
            streaming.STREAMS[fake_stream_id] = fake_queue
            # Must not raise TypeError
            streaming._run_agent_streaming(
                session_id="sess-compat-test",
                msg_text="hello",
                model="gpt-4o",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )

        # Agent was constructed successfully
        self.assertIn("session_id", captured["init_kwargs"])
        self.assertEqual(captured["init_kwargs"]["session_id"], "sess-compat-test")


class TestAgentCacheCredentialPoolStability(unittest.TestCase):
    """Credential-pool token churn must not evict cached WebUI agents."""

    def test_credential_pool_signature_ignores_volatile_runtime_token(self):
        import api.streaming as streaming

        pool = object()
        self.assertEqual(
            streaming._agent_cache_api_key_sig('token-a', pool),
            streaming._agent_cache_api_key_sig('token-b', pool),
        )
        self.assertNotEqual(
            streaming._agent_cache_api_key_sig('token-a', None),
            streaming._agent_cache_api_key_sig('token-b', None),
        )

    def test_cached_agent_runtime_refresh_swaps_key_without_losing_agent_state(self):
        import api.streaming as streaming

        class FakeAgent:
            def __init__(self):
                self.api_key = 'old-token'
                self.base_url = 'https://chatgpt.com/backend-api/codex'
                self.api_mode = 'codex_responses'
                self._client_kwargs = {
                    'api_key': 'old-token',
                    'base_url': self.base_url,
                    'default_headers': {'old': 'header'},
                }
                self._credential_pool = 'old-pool'
                self.context_compressor = type('Compressor', (), {
                    'base_url': self.base_url,
                    'api_key': 'old-token',
                })()
                self._primary_runtime = {
                    'base_url': self.base_url,
                    'api_key': 'old-token',
                    'client_kwargs': dict(self._client_kwargs),
                    'compressor_base_url': self.base_url,
                    'compressor_api_key': 'old-token',
                }
                self.header_refreshes = []
                self.replacements = []
                self.prefetch_survives = object()

            def _apply_client_headers_for_base_url(self, base_url):
                self.header_refreshes.append((base_url, self._client_kwargs['api_key']))
                self._client_kwargs['default_headers'] = {'refreshed-for': self._client_kwargs['api_key']}

            def _replace_primary_openai_client(self, *, reason):
                self.replacements.append(reason)
                return True

        agent = FakeAgent()
        preserved = agent.prefetch_survives
        changed = streaming._refresh_cached_agent_runtime(agent, {
            'api_key': 'new-token',
            'base_url': 'https://chatgpt.com/backend-api/codex',
            'credential_pool': 'new-pool',
        })

        self.assertTrue(changed)
        self.assertIs(agent.prefetch_survives, preserved)
        self.assertEqual(agent.api_key, 'new-token')
        self.assertEqual(agent._client_kwargs['api_key'], 'new-token')
        self.assertEqual(agent._credential_pool, 'new-pool')
        self.assertEqual(agent._primary_runtime['api_key'], 'new-token')
        self.assertEqual(agent._primary_runtime['client_kwargs']['api_key'], 'new-token')
        self.assertEqual(agent._primary_runtime['compressor_api_key'], 'new-token')
        self.assertEqual(getattr(agent.context_compressor, 'api_key'), 'new-token')
        self.assertEqual(agent.header_refreshes, [('https://chatgpt.com/backend-api/codex', 'new-token')])
        self.assertEqual(agent.replacements, ['webui_credential_refresh'])

    def test_same_key_refresh_repairs_stale_primary_runtime_snapshot(self):
        import api.streaming as streaming

        class FakeAgent:
            api_key = 'current-token'
            base_url = 'https://chatgpt.com/backend-api/codex'
            api_mode = 'codex_responses'
            _client_kwargs = {
                'api_key': 'current-token',
                'base_url': 'https://chatgpt.com/backend-api/codex',
            }
            _primary_runtime = {
                'api_key': 'old-token',
                'base_url': 'https://chatgpt.com/backend-api/codex',
                'client_kwargs': {
                    'api_key': 'old-token',
                    'base_url': 'https://chatgpt.com/backend-api/codex',
                },
            }

        agent = FakeAgent()
        ok = streaming._refresh_cached_agent_runtime(agent, {'api_key': 'current-token'})

        self.assertTrue(ok)
        self.assertEqual(agent._primary_runtime['api_key'], 'current-token')
        self.assertEqual(agent._primary_runtime['client_kwargs']['api_key'], 'current-token')

    def test_fallback_active_refresh_requests_rebuild_without_mutating_fallback(self):
        import api.streaming as streaming

        class FakeAgent:
            api_key = 'fallback-token'
            base_url = 'https://fallback.example/v1'
            api_mode = 'codex_responses'
            _fallback_activated = True
            _client_kwargs = {
                'api_key': 'fallback-token',
                'base_url': 'https://fallback.example/v1',
            }
            _primary_runtime = {
                'api_key': 'old-primary-token',
                'base_url': 'https://chatgpt.com/backend-api/codex',
                'client_kwargs': {
                    'api_key': 'old-primary-token',
                    'base_url': 'https://chatgpt.com/backend-api/codex',
                },
                'compressor_api_key': 'old-primary-token',
                'compressor_base_url': 'https://chatgpt.com/backend-api/codex',
            }

        agent = FakeAgent()
        ok = streaming._refresh_cached_agent_runtime(agent, {
            'api_key': 'new-primary-token',
            'base_url': 'https://chatgpt.com/backend-api/codex',
        })

        self.assertFalse(ok)
        self.assertEqual(agent.api_key, 'fallback-token')
        self.assertEqual(agent._client_kwargs['api_key'], 'fallback-token')
        self.assertEqual(agent._primary_runtime['api_key'], 'old-primary-token')
        self.assertEqual(agent._primary_runtime['client_kwargs']['api_key'], 'old-primary-token')
