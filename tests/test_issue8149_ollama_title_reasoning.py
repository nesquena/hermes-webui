"""#8149 — outgoing and surviving shape of the aux title reasoning-off inject.

``generate_title_raw_via_aux()`` injects a reasoning-off object into the aux
title call's ``extra_body``. What we send, and what survives hermes-agent's
merge, is pinned here:

  * On an Ollama route (provider ``ollama*``, base_url port 11434, or an
    ``ollama.com`` host) the inject is ``{"enabled": False, "effort": "none"}``
    unless the task config already carries a meaningful reasoning setting, in
    which case the inject is skipped.
  * Task-config keys are matched exactly. hermes-agent forwards the task
    config's keys verbatim, so a key spelled with different case or surrounding
    whitespace is not a setting the provider understands — it is not an
    override, and the inject is still sent.
  * Every non-Ollama route keeps master's unconditional bare inject
    ``{"enabled": False}``, even when the task config sets ``reasoning`` /
    ``reasoning_effort``. Those task shapes (e.g. OpenRouter
    ``reasoning: {exclude: true}``) do not suppress generation, while the bare
    inject does.
  * When the task config's ``extra_body`` already carries a meaningful
    reasoning setting on an Ollama route, skipping the inject keeps
    ``effective_extra_body.update(extra_body)`` from replacing it wholesale.

On current hermes-agent builds the injected object is not sent verbatim:
``call_llm`` / ``_build_call_kwargs`` pops an ``extra_body["reasoning"]`` that
carries an ``enabled`` key and promotes it to ``reasoning_config``, which
``_merge_aux_extra_body`` lets the provider profile project onto its own wire
(local Ollama: top-level ``reasoning_effort: "none"`` plus ``think: false``;
Ollama Cloud: ``reasoning_effort: "none"``). The explicit ``effort: "none"``
member only reaches the wire on builds or direct routes that ship
``extra_body.reasoning`` as-is. These tests pin the request body produced by
that documented path, not a claim that current Agents burn title tokens because
of this inject. Issue references: #2083, #4161, #8149.
"""
from __future__ import annotations

import json
import types
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config

OLLAMA_INJECT = {'enabled': False, 'effort': 'none'}
GENERIC_INJECT = {'enabled': False}

# The three positively identified Ollama shapes (#8149): provider id, a custom
# provider on Ollama's default local port, and Ollama Cloud.
OLLAMA_ROUTE_SHAPES = {
    'provider-ollama': {'provider': 'ollama', 'model': 'gemma4:26b', 'base_url': ''},
    'custom-port-11434': {
        'provider': 'custom',
        'model': 'gemma4:26b',
        'base_url': 'http://127.0.0.1:11434/v1',
    },
    'ollama-cloud': {
        'provider': '',
        'model': 'gemma4:26b',
        'base_url': 'https://api.ollama.com/v1',
    },
}
OLLAMA_ROUTE_PARAMS = [
    pytest.param(route, id=name) for name, route in OLLAMA_ROUTE_SHAPES.items()
]


@pytest.fixture(autouse=True)
def _install_auxiliary_client_modules():
    """Scope the synthetic hermes-agent modules to each test (#6630)."""
    with auxiliary_client_modules():
        yield


def _run_title_call(tg_config):
    """Run generate_title_raw_via_aux() with call_llm captured; return its kwargs."""
    from api.streaming import generate_title_raw_via_aux

    mock_resp = types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content='Test Title'),
                finish_reason='stop',
            )
        ]
    )
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return mock_resp

    with patch_tg_config(tg_config):
        with patch('agent.auxiliary_client.call_llm', side_effect=fake_call_llm, create=True):
            result, status = generate_title_raw_via_aux(
                user_text='What is the weather?',
                assistant_text='It is sunny.',
            )
    assert result == 'Test Title'
    assert status == 'llm_aux'
    return captured


def _merged_extra_body(tg_config):
    """Outgoing extra_body after hermes-agent's documented merge ordering.

    call_llm builds `effective_extra_body = _get_task_extra_body(task)` and
    then `effective_extra_body.update(extra_body or {})` — the caller's
    (WebUI's) keys win over the task config's. This helper reproduces that
    merge so tests can assert which of the user's settings survive it.
    """
    caller_extra = _run_title_call(tg_config).get('extra_body') or {}
    task_extra = tg_config.get('extra_body')
    merged = dict(task_extra) if isinstance(task_extra, dict) else {}
    merged.update(caller_extra)
    return merged


_ABSENT = object()


def _is_ollama_cloud_route(tg_config):
    host = (urlsplit(str(tg_config.get('base_url') or '').strip()).hostname or '').lower()
    return host == 'ollama.com' or host.endswith('.ollama.com')


def _ollama_wire_body(tg_config):
    """The JSON body an Ollama provider receives for a reasoning-off title call.

    Reproduces the path documented in the module header: hermes-agent merges the
    caller's ``extra_body`` over the task body, pops an ``extra_body.reasoning``
    object carrying an ``enabled`` key into ``reasoning_config``, and the
    provider profile projects a disabled config onto its own wire — local
    Ollama: ``reasoning_effort: "none"`` plus ``think: false``; Ollama Cloud:
    ``reasoning_effort: "none"``. The JSON round-trip mirrors SDK serialisation.
    """
    body = _merged_extra_body(tg_config)
    reasoning = body.pop('reasoning', None)
    if isinstance(reasoning, dict) and reasoning.get('enabled') is False:
        body['reasoning_effort'] = 'none'
        if not _is_ollama_cloud_route(tg_config):
            body['think'] = False
    elif reasoning is not None:
        body['reasoning'] = reasoning
    return json.loads(json.dumps(body))


def _route_config(route, extra_body):
    config = dict(route)
    if extra_body is not _ABSENT:
        config['extra_body'] = extra_body
    return config


def _ollama_config(extra_body):
    return _route_config(OLLAMA_ROUTE_SHAPES['provider-ollama'], extra_body)


def _generic_config(extra_body):
    config = {
        'provider': 'lmstudio',
        'model': 'qwen3-8b',
        'base_url': 'http://localhost:1234/v1',
    }
    if extra_body is not _ABSENT:
        config['extra_body'] = extra_body
    return config


def _injected_reasoning(tg_config):
    return (_run_title_call(tg_config).get('extra_body') or {}).get('reasoning')


class TestOllamaRouteGetsHonoredFlag:
    """The injected reasoning object for Ollama routes must carry effort:"none"."""

    def test_provider_ollama_sends_effort_none(self):
        reasoning = _injected_reasoning(
            {'provider': 'ollama', 'model': 'gemma4:26b', 'base_url': ''}
        )
        assert reasoning == OLLAMA_INJECT

    def test_local_base_url_port_11434_sends_effort_none(self):
        # Custom-provider lane: Ollama identified by its default local port.
        reasoning = _injected_reasoning({
            'provider': 'custom',
            'model': 'gemma4:26b',
            'base_url': 'http://127.0.0.1:11434/v1',
        })
        assert reasoning == OLLAMA_INJECT

    def test_ollama_cloud_host_sends_effort_none(self):
        reasoning = _injected_reasoning({
            'provider': '',
            'model': 'gemma4:26b',
            'base_url': 'https://api.ollama.com/v1',
        })
        assert reasoning == OLLAMA_INJECT

    def test_path_containing_11434_is_not_ollama(self):
        # Port match, not substring: a proxy whose PATH contains 11434 is a
        # generic route and keeps the unchanged suppression inject.
        reasoning = _injected_reasoning({
            'provider': 'custom',
            'model': 'gemma4:26b',
            'base_url': 'https://proxy.example.test/11434/v1',
        })
        assert reasoning == GENERIC_INJECT


class TestOllamaMeaningfulTaskConfigSkipsInject:
    """A real reasoning setting in the task config wins over the Ollama inject."""

    @pytest.mark.parametrize('route', OLLAMA_ROUTE_PARAMS)
    @pytest.mark.parametrize(
        'extra_body',
        [
            {'reasoning': {'enabled': True}},
            {'reasoning': {'enabled': False}},
            {'reasoning': {'effort': 'high'}},
            {'reasoning': {'max_tokens': 128}},
            {'reasoning_effort': 'high'},
            {'think': True},
            {'think': False},
            {'enable_thinking': False},
            {'thinking': 'high'},
            {'thinking': {'budget_tokens': 128}},
            {'thinking_budget': 512},
        ],
        ids=[
            'reasoning-enabled-true',
            'reasoning-enabled-false',
            'reasoning-effort',
            'reasoning-max-tokens',
            'reasoning-effort-top-level',
            'think-true',
            'think-false',
            'enable-thinking-false',
            'thinking-string',
            'thinking-object',
            'thinking-budget',
        ],
    )
    def test_meaningful_task_reasoning_setting_skips_ollama_inject(
        self, route, extra_body
    ):
        config = _route_config(route, extra_body)
        captured = _run_title_call(config)
        assert 'reasoning' not in (captured.get('extra_body') or {})
        merged = _merged_extra_body(config)
        for key, value in extra_body.items():
            assert merged.get(key) == value

    @pytest.mark.parametrize('route', OLLAMA_ROUTE_PARAMS)
    @pytest.mark.parametrize(
        'extra_body',
        [
            {' Reasoning_Effort ': 'none'},
            {' REASONING ': {'enabled': False}},
            {'THINK': False},
        ],
        ids=['stripped-case', 'capitalized-reasoning', 'capitalized-think'],
    )
    def test_malformed_key_does_not_suppress_ollama_reasoning_off_inject(
        self, route, extra_body
    ):
        # The malformed key is forwarded verbatim and means nothing to the
        # provider, so it must not suppress the inject: the request body has to
        # carry a reasoning-off setting the provider actually understands.
        body = _ollama_wire_body(_route_config(route, extra_body))
        assert body.get('reasoning_effort') == 'none'
        if not _is_ollama_cloud_route(route):
            assert body.get('think') is False

    def test_user_reasoning_effort_none_survives_merge_on_ollama(self):
        tg = _ollama_config({'reasoning_effort': 'none'})
        merged = _merged_extra_body(tg)
        assert merged.get('reasoning_effort') == 'none'
        assert 'reasoning' not in merged


class TestOllamaEmptyTaskConfigStillInjects:
    """Empty or null task-config values are not overrides; Ollama keeps its inject."""

    @pytest.mark.parametrize(
        'extra_body',
        [
            {},
            {'reasoning': None},
            {'reasoning': {}},
            {'reasoning': {'enabled': None}},
            {'reasoning': {'effort': ''}},
            {'reasoning': {'effort': '   '}},
            {'reasoning': 'high'},
            {'reasoning_effort': ''},
            {'reasoning_effort': '   '},
            {'thinking': ''},
            {'thinking': {}},
            {'thinking_budget': 0},
            {'thinking_budget': -5},
        ],
        ids=[
            'empty-extra-body',
            'reasoning-null',
            'reasoning-empty-object',
            'reasoning-enabled-null',
            'reasoning-effort-empty',
            'reasoning-effort-whitespace',
            'reasoning-non-dict',
            'reasoning-effort-top-level-empty',
            'reasoning-effort-top-level-whitespace',
            'thinking-empty-string',
            'thinking-empty-object',
            'thinking-budget-zero',
            'thinking-budget-negative',
        ],
    )
    def test_empty_task_reasoning_setting_keeps_ollama_inject(self, extra_body):
        reasoning = _injected_reasoning(_ollama_config(extra_body))
        assert reasoning == OLLAMA_INJECT

    def test_absent_extra_body_keeps_ollama_inject(self):
        reasoning = _injected_reasoning(_ollama_config(_ABSENT))
        assert reasoning == OLLAMA_INJECT

    @pytest.mark.parametrize('extra_body', ['not-a-dict', ['reasoning'], 42])
    def test_non_dict_extra_body_keeps_ollama_inject(self, extra_body):
        reasoning = _injected_reasoning(_ollama_config(extra_body))
        assert reasoning == OLLAMA_INJECT


class TestGenericRouteKeepsMasterInject:
    """Non-Ollama routes always get the bare inject, even with task settings."""

    def test_generic_route_replaces_user_reasoning_object_with_bare_disable(self):
        tg = _generic_config({'reasoning': {'exclude': True}})
        captured = _run_title_call(tg)
        # Master's unconditional suppression inject wins the merge.
        assert (captured.get('extra_body') or {}).get('reasoning') == GENERIC_INJECT
        merged = _merged_extra_body(tg)
        assert merged.get('reasoning') == GENERIC_INJECT

    def test_generic_route_keeps_user_reasoning_effort_beside_bare_disable(self):
        tg = _generic_config({'reasoning_effort': 'low'})
        merged = _merged_extra_body(tg)
        assert merged.get('reasoning') == GENERIC_INJECT
        assert merged.get('reasoning_effort') == 'low'

    @pytest.mark.parametrize(
        'extra_body',
        [_ABSENT, {}, {'reasoning': None}, {'reasoning': {}}, {'reasoning_effort': ''}],
        ids=['absent', 'empty-object', 'reasoning-null', 'reasoning-empty', 'effort-empty'],
    )
    def test_empty_or_missing_task_config_still_gets_bare_inject(self, extra_body):
        reasoning = _injected_reasoning(_generic_config(extra_body))
        assert reasoning == GENERIC_INJECT


class TestOtherRoutesUnchanged:
    """Suppression on non-Ollama routes is preserved (no global effort:"none")."""

    def test_openrouter_non_anthropic_keeps_bare_enabled_false(self):
        captured = _run_title_call({
            'provider': 'openrouter',
            'model': 'deepseek/deepseek-r1',
            'base_url': 'https://openrouter.ai/api/v1',
        })
        assert (captured.get('extra_body') or {}).get('reasoning') == GENERIC_INJECT
        assert 'reasoning_effort' not in (captured.get('extra_body') or {})

    def test_lmstudio_route_keeps_bare_enabled_false(self):
        reasoning = _injected_reasoning({
            'provider': 'lmstudio',
            'model': 'qwen3-8b',
            'base_url': 'http://localhost:1234/v1',
        })
        assert reasoning == GENERIC_INJECT

    def test_openai_route_still_skips_inject(self):
        captured = _run_title_call({
            'provider': 'openai',
            'model': 'gpt-5.5',
            'base_url': 'https://api.openai.com/v1',
        })
        assert 'reasoning' not in (captured.get('extra_body') or {})

    def test_azure_route_still_skips_inject(self):
        captured = _run_title_call({
            'provider': 'azure',
            'model': 'gpt-4',
            'base_url': 'https://x.openai.azure.com/',
        })
        assert 'reasoning' not in (captured.get('extra_body') or {})

    def test_openrouter_anthropic_route_still_skips_inject(self):
        captured = _run_title_call({
            'provider': 'openrouter',
            'model': 'anthropic/claude-sonnet-4.6',
            'base_url': 'https://openrouter.ai/api/v1',
        })
        assert 'reasoning' not in (captured.get('extra_body') or {})
