import sys
import types
from pathlib import Path


def _install_fake_auxiliary_client(fake_call_llm):
    agent_mod = sys.modules.get('agent') or types.ModuleType('agent')
    aux_mod = types.ModuleType('agent.auxiliary_client')
    aux_mod.call_llm = fake_call_llm
    agent_mod.auxiliary_client = aux_mod
    sys.modules['agent'] = agent_mod
    sys.modules['agent.auxiliary_client'] = aux_mod


def test_prompt_enhancement_default_route_uses_kimi_and_large_limits():
    from api import routes

    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return {
            'choices': [
                {'message': {'content': 'Please inspect the failing test and propose a minimal fix.'}}
            ]
        }

    _install_fake_auxiliary_client(fake_call_llm)
    enhanced, _, provider, model, route_label = routes._generate_prompt_enhancement(
        'fix test pls',
        workspace='/root/hermes-webui',
    )

    assert enhanced == 'Please inspect the failing test and propose a minimal fix.'
    assert provider == 'litellm-chat'
    assert model == 'fireworks/kimi-k2.6-turbo'
    assert route_label == 'Kimi K2.6 Turbo'
    assert captured['task'] == 'prompt_enhancement'
    assert captured['provider'] == 'litellm-chat'
    assert captured['model'] == 'fireworks/kimi-k2.6-turbo'
    assert captured['base_url'] == 'https://llm.dreamit.au/v1'
    assert captured['max_tokens'] == 131072
    assert captured['temperature'] == 0.2
    assert captured['timeout'] == 120.0


def test_prompt_enhancement_route_is_always_kimi():
    from api import routes

    provider, model, base_url, label = routes._prompt_enhancement_route()

    assert provider == 'litellm-chat'
    assert model == 'fireworks/kimi-k2.6-turbo'
    assert base_url == 'https://llm.dreamit.au/v1'
    assert label == 'Kimi K2.6 Turbo'


def test_prompt_enhancement_recent_context_uses_visible_user_and_assistant_messages():
    from api.routes import _prompt_enhancement_recent_context

    session = types.SimpleNamespace(messages=[
        {'role': 'system', 'content': 'ignore'},
        {'role': 'user', 'content': 'First request'},
        {'role': 'assistant', 'content': 'First answer'},
        {'role': 'tool', 'content': 'ignore tool'},
        {'role': 'user', 'content': 'Follow-up request'},
    ])

    context = _prompt_enhancement_recent_context(session, max_messages=4, max_chars=500)
    assert 'User: First request' in context
    assert 'Assistant: First answer' in context
    assert 'User: Follow-up request' in context
    assert 'ignore tool' not in context


def test_prompt_enhancement_cleans_common_preamble_and_fences():
    from api.routes import _clean_prompt_enhancement

    raw = '```markdown\nEnhanced prompt: Please review `/root/hermes-webui` and list the issue.\n```'
    assert _clean_prompt_enhancement(raw) == 'Please review `/root/hermes-webui` and list the issue.'


def test_prompt_enhance_endpoint_is_registered_in_post_router():
    src = Path('api/routes.py').read_text(encoding='utf-8')
    assert 'if parsed.path == "/api/prompt/enhance"' in src
    assert 'return _handle_prompt_enhance(handler, body)' in src
    assert 'PROMPT_ENHANCE_MAX_OUTPUT_TOKENS = 131072' in src
    assert 'preview_label' in src
    assert 'session_context_used' in src
