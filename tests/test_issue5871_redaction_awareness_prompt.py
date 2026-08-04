"""Product-semantics coverage for WebUI credential-masking guidance (#5871)."""

import re

from api.streaming import _WEBUI_PROGRESS_PROMPT, _webui_ephemeral_system_prompt


def _redaction_guidance() -> str:
    return next(
        line for line in _WEBUI_PROGRESS_PROMPT.splitlines()
        if "automatically redacted" in line
    )


def test_progress_prompt_explains_intentional_credential_redaction():
    guidance = _redaction_guidance()

    assert "Password, API-key, token, and secret fields" in guidance
    assert "intentional redaction" in guidance
    assert "not placeholder text or user input errors" in guidance
    assert "do not tell the user a stored credential is wrong" in guidance
    assert "masked value alone" in guidance


def test_redaction_guidance_flows_into_ephemeral_system_prompt():
    prompt = _webui_ephemeral_system_prompt(
        "Use a concise tone.",
        surface_context={"source": "webui"},
    )

    assert _redaction_guidance() in prompt


def test_progress_prompt_retains_secret_leak_prevention_guidance():
    assert (
        "Do not reveal hidden reasoning, chain-of-thought, private scratchpads, "
        "secrets, raw logs, or long tool output."
    ) in _WEBUI_PROGRESS_PROMPT


def test_redaction_guidance_does_not_teach_concrete_key_or_mask_formats():
    guidance = _redaction_guidance()

    assert "***" not in guidance
    assert "..." not in guidance
    assert not re.search(r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)\S+", guidance)
    assert not re.search(r"\b[A-Za-z0-9_-]{24,}\b", guidance)
