"""Issue #8149 — Ollama ignores the title-gen reasoning-off flag and cancels the user's setting.

`generate_title_raw_via_aux()` injects `extra_body={"reasoning": {"enabled": False}}`.
On Ollama's OpenAI-compatible `/v1` endpoint (measured, issue #8149):

  * `reasoning: {"enabled": false}` alone is IGNORED — the model reasons anyway
    (353 completion tokens vs 498 with nothing sent).
  * top-level `reasoning_effort: "none"` and `reasoning.effort: "none"` DO turn
    reasoning off (5 tokens).
  * worst of all, sending the `reasoning` object NEXT TO a user-configured
    `reasoning_effort: "none"` (via `auxiliary.title_generation.extra_body`)
    re-enables reasoning (269 tokens) — the flag meant to disable it cancels
    the user's working setting.

Contract pinned here (outgoing aux title call):

  1. A positively-identified Ollama route (provider `ollama*`, base_url port
     11434, or an ollama.com host) must receive `reasoning.effort: "none"` in
     the injected object, not the bare `{"enabled": False}` Ollama ignores.
  2. When the task config's own `extra_body` already carries a reasoning
     setting (`reasoning` / `reasoning_effort`), the inject must be skipped
     entirely — hermes-agent's `call_llm` merges the caller's extra_body ON
     TOP of the task extra_body (`effective_extra_body.update(extra_body)`),
     so an injected key would replace the user's setting.
  3. Non-Ollama routes keep the existing suppression inject unchanged
     (`reasoning: {"enabled": False}`), per the #4161 gate's contract.

The hermes-agent-side merge is simulated with the documented ordering;
these tests pin the WebUI side of that contract.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config


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
    merge so tests can assert the user's settings survive it.
    """
    caller_extra = _run_title_call(tg_config).get('extra_body') or {}
    task_extra = tg_config.get('extra_body') or {}
    merged = dict(task_extra)
    merged.update(caller_extra)
    return merged


class TestOllamaRouteGetsHonoredFlag:
    """The injected reasoning object for Ollama routes must carry effort:"none"."""

    def test_provider_ollama_sends_effort_none(self):
        captured = _run_title_call({'provider': 'ollama', 'model': 'gemma4:26b', 'base_url': ''})
        reasoning = (captured.get('extra_body') or {}).get('reasoning')
        # Bare {"enabled": False} is exactly what Ollama ignores (#8149).
        assert reasoning != {'enabled': False}
        assert reasoning.get('effort') == 'none'
        assert reasoning.get('enabled') is False

    def test_local_base_url_port_11434_sends_effort_none(self):
        # Custom-provider lane: Ollama identified by its default local port.
        captured = _run_title_call({
            'provider': 'custom',
            'model': 'gemma4:26b',
            'base_url': 'http://127.0.0.1:11434/v1',
        })
        reasoning = (captured.get('extra_body') or {}).get('reasoning')
        assert reasoning is not None
        assert reasoning.get('effort') == 'none'

    def test_ollama_cloud_host_sends_effort_none(self):
        captured = _run_title_call({
            'provider': '',
            'model': 'gemma4:26b',
            'base_url': 'https://api.ollama.com/v1',
        })
        reasoning = (captured.get('extra_body') or {}).get('reasoning')
        assert reasoning is not None
        assert reasoning.get('effort') == 'none'

    def test_path_containing_11434_is_not_ollama(self):
        # Port match, not substring: a proxy whose PATH contains 11434 is a
        # generic route and keeps the unchanged suppression inject.
        captured = _run_title_call({
            'provider': 'custom',
            'model': 'gemma4:26b',
            'base_url': 'https://proxy.example.test/11434/v1',
        })
        reasoning = (captured.get('extra_body') or {}).get('reasoning')
        assert reasoning == {'enabled': False}


class TestUserReasoningConfigSurvives:
    """The inject must not clobber a reasoning setting the task config already carries."""

    def test_user_reasoning_effort_none_survives_merge_on_ollama(self):
        tg = {
            'provider': 'ollama',
            'model': 'gemma4:26b',
            'base_url': '',
            'extra_body': {'reasoning_effort': 'none'},
        }
        merged = _merged_extra_body(tg)
        # The exact cancellation #8149 measures: user's reasoning_effort must
        # still be in the merged payload after the caller's update() ran.
        assert merged.get('reasoning_effort') == 'none'
        # And the inject must not add the reasoning object that cancels it.
        assert 'reasoning' not in merged

    def test_user_reasoning_object_survives_merge_on_generic_route(self):
        tg = {
            'provider': 'openrouter',
            'model': 'deepseek/deepseek-r1',
            'base_url': 'https://openrouter.ai/api/v1',
            'extra_body': {'reasoning': {'effort': 'low'}},
        }
        merged = _merged_extra_body(tg)
        assert merged.get('reasoning') == {'effort': 'low'}

    def test_user_reasoning_effort_skips_inject_on_generic_route(self):
        captured = _run_title_call({
            'provider': 'openrouter',
            'model': 'deepseek/deepseek-r1',
            'base_url': 'https://openrouter.ai/api/v1',
            'extra_body': {'reasoning_effort': 'low'},
        })
        # Caller keys win the update(); an injected reasoning object would
        # coexist with (and on Ollama cancel) the user's effort setting.
        assert 'reasoning' not in (captured.get('extra_body') or {})


class TestOtherRoutesUnchanged:
    """Suppression on non-Ollama routes is preserved (no global effort:"none")."""

    def test_openrouter_non_anthropic_keeps_bare_enabled_false(self):
        captured = _run_title_call({
            'provider': 'openrouter',
            'model': 'deepseek/deepseek-r1',
            'base_url': 'https://openrouter.ai/api/v1',
        })
        assert (captured.get('extra_body') or {}).get('reasoning') == {'enabled': False}
        assert 'reasoning_effort' not in (captured.get('extra_body') or {})

    def test_lmstudio_route_keeps_bare_enabled_false(self):
        captured = _run_title_call({
            'provider': 'lmstudio',
            'model': 'qwen3-8b',
            'base_url': 'http://localhost:1234/v1',
        })
        assert (captured.get('extra_body') or {}).get('reasoning') == {'enabled': False}

    def test_openai_route_still_skips_inject(self):
        captured = _run_title_call({
            'provider': 'openai',
            'model': 'gpt-5.5',
            'base_url': 'https://api.openai.com/v1',
        })
        assert 'reasoning' not in (captured.get('extra_body') or {})
