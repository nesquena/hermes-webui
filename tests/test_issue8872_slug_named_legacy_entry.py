"""#8872: a legacy custom_providers entry whose name only slugs to a built-in id lends that built-in's sessions no key.

v0.52.544 matches a bare built-in session to a legacy entry by exact name only; exp-v0.52.597 (#8448) matched the
slug, so an entry named "OpenAI Codex" lent its key to openai-codex sessions (Agent v2026.9.24 then sent it to
chatgpt.com). Adapted from the round-9 senior review's tests for the restored v0.52.544 lookup: the narrowed
resolver-list assertion (#8448's contract) and the caller-URL-equals-entry-URL variant (v0.52.544 behaviour, the
#8822 provider-routed-host class) are not part of this rule.
"""
import pytest

import api.config as config
import api.routes as routes

_GW = "https://codex-proxy.invalid/v1"


def _use(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data, raising=False)
    monkeypatch.setattr(config, "get_config", lambda: data)


@pytest.mark.parametrize("name", ["OpenAI Codex", "openai codex", "OpenAI (Codex)"])
def test_a_legacy_entry_that_only_slugs_to_a_builtin_lends_it_no_key(monkeypatch, name):
    data = {"custom_providers": [{"name": name, "base_url": _GW, "api_key": "synthetic-proxy"}]}
    _use(monkeypatch, data)
    got = routes._context_length_lookup_inputs_for_model("gpt-5.5", "openai-codex", cfg=data)
    assert got.api_key == ""
    assert got.base_url == ""


@pytest.mark.parametrize("name", ["openai-codex", "OPENAI-CODEX"])
def test_a_legacy_entry_named_as_the_builtin_still_claims_it(monkeypatch, name):
    data = {"custom_providers": [{"name": name, "base_url": _GW, "api_key": "synthetic-proxy"}]}
    _use(monkeypatch, data)
    got = routes._context_length_lookup_inputs_for_model("gpt-5.5", "openai-codex", cfg=data)
    assert (got.base_url, got.api_key, got.provider) == (_GW, "synthetic-proxy", "openai-codex")
