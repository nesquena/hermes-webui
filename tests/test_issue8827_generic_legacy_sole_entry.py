"""#8827: a generic session (custom/local/vllm/ollama) with a sole legacy custom_providers entry and no URL resolves
that entry's URL and key for the context-length lookup again, as v0.52.544 did (#8819 restores 544's lookup)."""

import pytest

import api.config as config
import api.routes as routes
from tests.conftest import requires_agent

ENTRY_URL = "https://entry.invalid/v1"
MODEL_URL = "https://model-block.invalid/v1"
ENTRY = {"name": "local", "base_url": ENTRY_URL, "api_key": "K-entry"}


def _lookup(monkeypatch, cfg, session, **kwargs):
    cfg.setdefault("model", {})
    cfg.setdefault("providers", {})
    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    r = routes._context_length_lookup_inputs_for_model("m-x", session, cfg=cfg, **kwargs)
    return (r.base_url or "").rstrip("/"), r.api_key or ""


# vllm and ollama are generic sessions only through the Agent's provider tables; without an Agent (CI),
# v0.52.544 also returns no URL or key for them, so those two need the Agent.
@pytest.mark.parametrize(
    "session",
    ["custom", "local", pytest.param("vllm", marks=requires_agent),
     pytest.param("ollama", marks=requires_agent)],
)
@pytest.mark.parametrize("name", ["local", "My LLM"])
def test_generic_session_takes_the_sole_legacy_entry(monkeypatch, session, name):
    got = _lookup(monkeypatch, {"custom_providers": [dict(ENTRY, name=name)]}, session)
    assert got == (ENTRY_URL, "K-entry")


@pytest.mark.parametrize("session", ["local", "custom"])
def test_two_legacy_entries_stay_unresolved(monkeypatch, session):
    other = {"name": "other", "base_url": "https://other.invalid/v1", "api_key": "K-other"}
    assert _lookup(monkeypatch, {"custom_providers": [dict(ENTRY), other]}, session) == ("", "")


def test_a_native_record_alone_is_not_adopted(monkeypatch):
    cfg = {"providers": {"custom:local": {"base_url": ENTRY_URL, "api_key": "K-native"}}, "custom_providers": []}
    assert _lookup(monkeypatch, cfg, "custom") == ("", "")
