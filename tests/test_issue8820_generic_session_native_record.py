"""#8820: a generic session (no provider, the literal ``custom``, or an alias such as ``ollama``/``local``/``vllm``)
doesn't adopt a native ``providers['custom:<x>']`` record for the context-length lookup, by model name or by URL.
v0.52.544 never did; exp-v0.52.597..650 did (#8819 restores 544's lookup). Runs without an Agent, as CI does."""

import pytest

import api.config as config
import api.routes as routes

NAMED_URL = "https://named.invalid/v1"


@pytest.mark.parametrize("shape", ["model-only", "model-url-same"])
@pytest.mark.parametrize("session", [None, "custom", "ollama", "local", "vllm"])
def test_a_generic_session_does_not_adopt_a_native_record(monkeypatch, session, shape):
    record = "custom:" + (session or "custom")
    cfg = {
        "model": {"default": "m-x"},
        "providers": {record: {"base_url": NAMED_URL, "api_key": "synthetic-named",
                               "models": {"m-x": {"context_length": 12345}}}},
        "custom_providers": [],
    }
    if session:
        cfg["model"]["provider"] = session
    if shape == "model-url-same":
        cfg["model"]["base_url"] = NAMED_URL
    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)

    got = routes._context_length_lookup_inputs_for_model("m-x", session, cfg=cfg)
    assert got.api_key != "synthetic-named", (session, shape, got.base_url)
    assert got.config_context_length != 12345, (session, shape)
