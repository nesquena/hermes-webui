"""agent v0.21 transport-API port tests for out-of-band LLM completions.

hermes-agent v0.21 removed ``agent.anthropic_adapter.normalize_anthropic_response``
and ``AIAgent._normalize_codex_response``. The sanctioned out-of-band contract is
``agent._get_transport(<mode>).normalize_response(...)`` returning a
``NormalizedResponse`` (``.content``).

These tests pin the two out-of-band consumers against a v0.21-shaped fake agent
(no legacy normalizer symbols exist; the transport normalizes):
  - ``api.streaming.generate_title_raw_via_agent`` (anthropic_messages branch)
  - the nested ``_agent_text_completion`` inside
    ``api.routes._handle_handoff_summary`` (codex_responses branch)

On the pre-port code both branches die (ImportError / AttributeError) and
silently fall back to empty titles / local fallback summaries; these tests
fail in that state.
"""
import sys
import types

TITLE_TEXT = 'Claude Transport Title'
SUMMARY_TEXT = '- The remaining work is the final review pass.'


class _FakeTransport:
    """v0.21-shaped transport: normalize_response returns a NormalizedResponse-like object."""

    def __init__(self, content):
        self._content = content
        self.calls = []

    def normalize_response(self, resp, **kwargs):
        self.calls.append((resp, kwargs))
        return types.SimpleNamespace(content=self._content)


def _make_anthropic_fake_agent(transport):
    """Fake agent exposing only the v0.21 surface the title branch needs."""

    class _FakeAgent:
        api_mode = 'anthropic_messages'
        model = 'anthropic/claude-sonnet-4-5'
        provider = 'anthropic'
        base_url = ''

        def __init__(self):
            self.reasoning_config = None
            self._is_anthropic_oauth = True

        def _anthropic_preserve_dots(self):
            return False

        def _anthropic_messages_create(self, api_kwargs):
            # Opaque response blob; normalizing is the transport's job on v0.21.
            return object()

        def _get_transport(self, mode=None):
            assert mode is None
            return transport

    return _FakeAgent()


def test_anthropic_title_uses_transport_normalize_response(monkeypatch):
    """The anthropic_messages title branch must normalize via the agent transport.

    v0.21 deleted ``normalize_anthropic_response``; the branch must not import it.
    """
    from api.streaming import generate_title_raw_via_agent

    # Simulate the v0.21 adapter module in-process (works in agent-less CI too):
    # it still exports build_anthropic_kwargs but deliberately does NOT define
    # normalize_anthropic_response — exactly the environment this port targets.
    fake_adapter = types.ModuleType('agent.anthropic_adapter')
    fake_adapter.build_anthropic_kwargs = lambda **kwargs: {}
    fake_agent_pkg = sys.modules.get('agent') or types.ModuleType('agent')
    fake_agent_pkg.anthropic_adapter = fake_adapter
    monkeypatch.setitem(sys.modules, 'agent', fake_agent_pkg)
    monkeypatch.setitem(sys.modules, 'agent.anthropic_adapter', fake_adapter)

    transport = _FakeTransport(TITLE_TEXT)
    agent = _make_anthropic_fake_agent(transport)

    result, status = generate_title_raw_via_agent(
        agent,
        user_text='Hey nur ein kurzer Test',
        assistant_text='Alles klar, ich helfe dir dabei.',
    )

    assert result == TITLE_TEXT
    assert status == 'llm'
    assert transport.calls, 'transport normalize_response must be called'
    assert transport.calls[0][1].get('strip_tool_prefix') is True


def test_codex_handoff_summary_uses_transport_normalize_response(monkeypatch):
    """The codex_responses handoff branch must normalize via the agent transport.

    v0.21 deleted ``AIAgent._normalize_codex_response``; the branch must use
    ``agent._get_transport('codex_responses').normalize_response(...)``.
    """
    import api.config as cfg
    import api.models as models
    import api.routes as routes

    monkeypatch.setattr(routes, 'require', lambda body, *keys: None)
    monkeypatch.setattr(
        routes,
        'bad',
        lambda _handler, msg, status=400: {'ok': False, 'error': msg, 'status': status},
    )
    monkeypatch.setattr(
        routes,
        'j',
        lambda _handler, payload, status=200, extra_headers=None: payload,
    )
    monkeypatch.setattr(
        routes,
        '_persist_handoff_summary',
        lambda sid, summary, channel, rounds, fallback=False: persisted.append(
            {
                'sid': sid,
                'summary': summary,
                'channel': channel,
                'rounds': rounds,
                'fallback': fallback,
            }
        )
        or {'ok': True},
    )
    monkeypatch.setattr(
        models,
        'count_conversation_rounds',
        lambda sid, since=None: models.CONVERSATION_ROUND_THRESHOLD,
    )
    monkeypatch.setattr(
        models,
        'get_cli_session_messages',
        lambda sid: [
            {'role': 'user', 'content': 'What remains to do?', 'timestamp': 1.0},
            {'role': 'assistant', 'content': 'One review step remains.', 'timestamp': 2.0},
            {'role': 'user', 'content': 'And after that?', 'timestamp': 3.0},
            {'role': 'assistant', 'content': 'Then we ship.', 'timestamp': 4.0},
        ],
    )
    monkeypatch.setattr(
        cfg,
        'resolve_model_provider',
        lambda resolved_model=None: (
            'gpt-test',
            'openai-codex',
            'https://chatgpt.com/backend-api/codex',
        ),
    )

    persisted = []
    transport = _FakeTransport(SUMMARY_TEXT)

    class _CodexAgent:
        api_mode = 'codex_responses'

        def __init__(self, *args, **kwargs):
            self.model = kwargs.get('model')
            self.provider = kwargs.get('provider')
            self.base_url = kwargs.get('base_url')
            self.reasoning_config = None

        def _build_api_kwargs(self, api_messages):
            return {'model': self.model, 'instructions': 'summary', 'input': [], 'store': False}

        def _run_codex_stream(self, kwargs):
            # Opaque response blob; normalizing is the transport's job on v0.21.
            return object()

        def _get_transport(self, mode=None):
            assert mode == 'codex_responses'
            return transport

        def release_clients(self):
            return None

    # v0.21: the legacy method is gone from the agent.
    assert not hasattr(_CodexAgent, '_normalize_codex_response')

    fake_run_agent = types.ModuleType('run_agent')
    fake_run_agent.AIAgent = _CodexAgent
    monkeypatch.setitem(sys.modules, 'run_agent', fake_run_agent)

    fake_runtime_module = types.ModuleType('hermes_cli.runtime_provider')
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        'api_key': 'x',
        'provider': 'openai-codex',
        'base_url': 'https://chatgpt.com/backend-api/codex',
    }
    fake_hermes_cli = types.ModuleType('hermes_cli')
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes_cli)
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', fake_runtime_module)

    response = routes._handle_handoff_summary(
        object(), {'session_id': 'session-codex-v021-transport'}
    )

    assert response['ok'] is True
    assert response['fallback'] is False
    assert response['summary'] == SUMMARY_TEXT
    assert persisted, 'handoff summary must be persisted'
    assert persisted[0]['summary'] == SUMMARY_TEXT
    assert persisted[0]['fallback'] is False
