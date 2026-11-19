"""Reviewed counting probes with Hermes-authored pytest regressions."""
import re
import sys
import types
import pytest
from unittest.mock import patch

from api import helpers


class _CountingPattern:
    def __init__(self, pattern, calls):
        self.pattern = pattern
        self.calls = calls

    def __getattr__(self, name):
        return getattr(self.pattern, name)

    def sub(self, *args, **kwargs):
        self.calls.append("sub")
        return self.pattern.sub(*args, **kwargs)

    def finditer(self, *args, **kwargs):
        self.calls.append("finditer")
        return self.pattern.finditer(*args, **kwargs)


class _CountingRegex:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return getattr(re, name)

    def compile(self, pattern, flags=0):
        compiled = re.compile(pattern, flags)
        if "API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH" in pattern:
            return _CountingPattern(compiled, self.calls)
        return compiled


def _build(redactor=None):
    probe = _CountingRegex()
    agent = types.ModuleType("agent")
    module = types.ModuleType("agent.redact")
    if redactor is not None:
        module.redact_sensitive_text = redactor
    with patch.dict(sys.modules, {"agent": agent, "agent.redact": module}), patch.object(helpers, "_re", probe):
        return helpers._build_redact_fn(), probe


def _identity(text, *, force):
    assert force is True
    return text


@pytest.mark.parametrize("agent", [None, _identity])
def test_no_equals_skips_only_impossible_scans(agent):
    text = "https://example.invalid/ " + "A" * 20000
    redact, probe = _build(agent)
    assert helpers._might_contain_sensitive_text(text)
    with patch.object(helpers, "_redact_fn_uncached", redact):
        assert helpers._redact_text(text, _enabled=True) == text
    assert probe.calls == []


@pytest.mark.parametrize("agent", [None, _identity])
@pytest.mark.parametrize("key", ["API_KEY", "PASSWORD", "TOKEN"])
def test_assignments_still_mask_values(agent, key):
    redact, probe = _build(agent)
    value = "SYNTHETIC_VALUE_1234567890"
    assert value not in redact(f'{key}="{value}"')
    assert "sub" in probe.calls


@pytest.mark.parametrize("agent", [None, _identity])
@pytest.mark.parametrize("delimiter", ["\\u003d", "＝", "≠"])
def test_nonliteral_delimiters_do_not_create_assignment(agent, delimiter):
    redact, probe = _build(agent)
    text = "TOKEN" + delimiter + "SYNTHETIC字🙂"
    assert redact(text) == text
    assert probe.calls == []


@pytest.mark.parametrize("agent", [None, _identity])
def test_other_credential_families_remain_active(agent):
    redact, probe = _build(agent)
    credential = "s" + "k-" + "SYNTHETICTESTVALUE1234567890"
    auth = "SYNTHETIC_AUTH_VALUE_1234567890"
    pem = "-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END " + "PRIVATE KEY-----"
    for text in [credential, "Authorization: " + "Bearer " + auth, pem]:
        result = redact(text)
        assert result != text
        assert credential not in result and auth not in result and pem not in result
    assert probe.calls == []


def test_fallback_checks_actual_post_mask_text():
    redact, probe = _build()
    value = "SYNTHETIC=AUTH_VALUE_1234567890"
    result = redact("Authorization: " + "Bearer " + value)
    assert value not in result and "=" not in result
    assert probe.calls == []


def test_agent_introduced_assignment_still_masked():
    def introduce(text, *, force):
        assert force
        return "TOKEN=SYNTHETIC_VALUE_1234567890"
    redact, probe = _build(introduce)
    assert "SYNTHETIC_VALUE_1234567890" not in redact("no delimiter")
    assert probe.calls == ["sub"]


def test_agent_removed_assignment_checks_original_for_restoration():
    def remove(text, *, force):
        assert force
        return text.replace("=", "")
    redact, probe = _build(remove)
    assert redact("TOKEN=SYNTHETIC") == "TOKENSYNTHETIC"
    assert probe.calls == ["finditer"]


def test_occurrence_aware_code_literal_restoration():
    key, value = "SYNTHETIC_API_KEY", "SYNTHETIC_VALUE_1234567890"
    original = f'{key}={value}\nif line.startswith("{key}=\"):'
    def mask(text, *, force):
        assert force
        return text.replace(value, "***").replace(f'{key}=\"):', f"{key}=***")
    redact, _ = _build(mask)
    assert redact(original) == f'{key}=***\nif line.startswith("{key}=\"):'


def test_legacy_agent_remains_called():
    seen = []
    def legacy(text):
        seen.append(text)
        return "legacy-masked"
    redact, probe = _build(legacy)
    assert redact("https://example.invalid/") == "legacy-masked"
    assert seen == ["https://example.invalid/"] and probe.calls == []


@pytest.mark.parametrize("agent", [None, _identity])
def test_empty_and_nonstring_behavior_preserved(agent):
    redact, probe = _build(agent)
    for value in ["", None, 42, [], {}]:
        assert redact(value) is value
    assert probe.calls == []


def test_disabled_public_redaction_preserved():
    text = "API_KEY=SYNTHETIC_VALUE_1234567890"
    assert helpers._redact_text(text, _enabled=False) == text
