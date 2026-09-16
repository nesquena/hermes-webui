"""Regression coverage for the #7481 follow-up: one endpoint, one probe per rebuild.

The cold catalog rebuild can reach the same custom endpoint more than once. Step 4
probes the active ``model.base_url``, and the LM Studio provider-group fallback later
reads ``providers.lmstudio.base_url`` — which falls back to ``model.base_url`` when
lmstudio is the active provider. In the config shape the issue reports both point at
the same unreachable LAN host, so one dead endpoint used to cost the rebuild that
host's connect timeout TWICE, and two named ``custom_providers`` entries can share an
endpoint just as easily.

The fix memoises each probe by (endpoint URL, credential) for the duration of one
rebuild, so a repeated endpoint reuses the first outcome — including a failure, which
is exactly what used to stall the rebuild a second time — while a different URL or a
different credential is still probed in its own right.
"""

from __future__ import annotations

import socket

import pytest

import api.config as cfg
import api.profiles as profiles
from tests.test_issue7481_custom_probe_budget_fairness import (
    _GATEWAY_MODELS,
    _configure,
    _install_urlopen,
    _models_by_provider,
)


@pytest.fixture
def isolate_models_catalog_state(monkeypatch, tmp_path):
    """Hermetic catalog state, same harness as the sibling #7481 module.

    Defined here rather than imported from there: a fixture imported purely so it
    can be requested as a fixture reads as an unused import to linters (ruff F401)
    and collides with the very parameters that request it (ruff F811). Each test
    module owning its harness is what the rest of this suite does.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    auth_store_path = tmp_path / "auth.json"
    auth_store_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(cfg, "_get_auth_store_path", lambda: auth_store_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: "issue-7481-fp")
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "_models_rebuild_seq", 0, raising=False)
    monkeypatch.setattr(cfg, "_models_published_seq", 0, raising=False)
    monkeypatch.setattr(cfg, "cfg", {}, raising=False)
    # Any provider left in the catalog would otherwise shell out to the Hermes
    # CLI for a live id list; the rebuild must stay network-free apart from the
    # custom endpoints under test.
    monkeypatch.setattr(cfg, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path / "hermes-home")
    monkeypatch.setattr(cfg.os, "getenv", lambda key, default=None: default or "")
    # The probe path resolves the endpoint hostname for its SSRF guard. A real
    # resolver makes these tests depend on the host's DNS behaviour (and this
    # container takes seconds to answer NXDOMAIN), so pin it to an immediate
    # failure: the guard treats that as "not resolvable" and lets the probe
    # through, which is exactly what the fake urlopen above is standing in for.
    def _unresolvable(host, port, *args, **kwargs):
        raise socket.gaierror("hermetic test resolver")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)

    return {"tmp_path": tmp_path, "auth_store_path": auth_store_path}


def _hosts(observed, kind: str) -> list[str]:
    return [url.split("://", 1)[1].split("/", 1)[0] for url, _ in observed[kind]]


def test_shared_endpoint_between_active_probe_and_lmstudio_fallback_is_probed_once(
    monkeypatch, isolate_models_catalog_state
):
    """A dead host configured as both the active endpoint and lmstudio costs one probe.

    This is the issue's own config shape: before the de-duplication the same
    unreachable LAN host was probed by step 4 and again by the LM Studio
    provider-group fallback, so it spent two slices of the shared rebuild window.
    """
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        provider_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {
                "name": "My Gateway",
                "base_url": "https://gw-live.example/v1",
                "api_key": "sk-live",
            }
        ],
    )
    observed = _install_urlopen(
        monkeypatch, dead_hosts=["lan-dead.example"], live_hosts=["gw-live.example"]
    )

    catalog = cfg.get_available_models()

    assert _hosts(observed, "dead") == ["lan-dead.example:1234"], (
        "the shared dead endpoint was probed more than once"
    )
    # The reachable provider behind it still made it into the published catalog.
    assert _models_by_provider(catalog).get("custom:my-gateway") == _GATEWAY_MODELS


def test_shared_reachable_endpoint_is_probed_once_and_still_renders(
    monkeypatch, isolate_models_catalog_state
):
    """De-duplication must not cost the lmstudio group its models."""
    _configure(
        monkeypatch,
        active_base_url="http://lan-live.example:1234/v1",
        provider_base_url="http://lan-live.example:1234/v1",
    )
    observed = _install_urlopen(
        monkeypatch, dead_hosts=[], live_hosts=["lan-live.example:1234"]
    )

    catalog = cfg.get_available_models()

    assert _hosts(observed, "live") == ["lan-live.example:1234"], (
        "the shared reachable endpoint was probed more than once"
    )
    # The probed models still land in the lmstudio group (which also carries the
    # config-declared default model, so assert containment, not equality).
    assert set(_GATEWAY_MODELS) <= set(_models_by_provider(catalog).get("lmstudio") or [])


def test_distinct_endpoints_are_still_each_probed(
    monkeypatch, isolate_models_catalog_state
):
    """Two different endpoints are two probes — no over-merging."""
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead-a.example:1234/v1",
        provider_base_url="http://lan-dead-b.example:1234/v1",
    )
    observed = _install_urlopen(
        monkeypatch, dead_hosts=["lan-dead-a.example", "lan-dead-b.example"], live_hosts=[]
    )

    cfg.get_available_models()

    assert sorted(_hosts(observed, "dead")) == [
        "lan-dead-a.example:1234",
        "lan-dead-b.example:1234",
    ]


def test_same_endpoint_with_a_different_credential_is_probed_separately(
    monkeypatch, isolate_models_catalog_state
):
    """The credential is part of a probe's identity: it can change the outcome."""
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        provider_base_url="http://lan-dead.example:1234/v1",
    )
    cfg.cfg["model"]["api_key"] = "active-key"
    cfg.cfg["providers"]["lmstudio"]["api_key"] = "lmstudio-key"
    observed = _install_urlopen(
        monkeypatch, dead_hosts=["lan-dead.example"], live_hosts=[]
    )

    cfg.get_available_models()

    assert _hosts(observed, "dead") == [
        "lan-dead.example:1234",
        "lan-dead.example:1234",
    ], "one credential's result was reused for a probe that sends a different one"


def test_named_providers_sharing_an_endpoint_are_probed_once(
    monkeypatch, isolate_models_catalog_state
):
    """Two named entries pointing at one endpoint probe it once, for both groups."""
    _configure(
        monkeypatch,
        active_base_url=None,
        custom_providers=[
            {"name": "Proxy A", "base_url": "https://shared.example/v1", "api_key": "k"},
            {"name": "Proxy B", "base_url": "https://shared.example/v1", "api_key": "k"},
        ],
    )
    observed = _install_urlopen(monkeypatch, dead_hosts=[], live_hosts=["shared.example"])

    catalog = cfg.get_available_models()

    assert _hosts(observed, "live") == ["shared.example"]
    groups = _models_by_provider(catalog)
    assert groups.get("custom:proxy-a") == _GATEWAY_MODELS
    assert groups.get("custom:proxy-b") == _GATEWAY_MODELS


def test_named_provider_repeating_the_active_endpoint_is_probed_once(
    monkeypatch, isolate_models_catalog_state
):
    """The active-endpoint result is reused by a named entry on the same URL."""
    _configure(
        monkeypatch,
        active_base_url="http://lan-dead.example:1234/v1",
        custom_providers=[
            {
                "name": "Same Host",
                "base_url": "http://lan-dead.example:1234/v1",
                "api_key": "",
            }
        ],
    )
    observed = _install_urlopen(monkeypatch, dead_hosts=["lan-dead.example"], live_hosts=[])

    cfg.get_available_models()

    assert _hosts(observed, "dead") == ["lan-dead.example:1234"]


def test_trailing_slash_spelling_of_one_endpoint_is_a_single_probe(
    monkeypatch, isolate_models_catalog_state
):
    """`https://h/v1` and `https://h/v1/` are the same endpoint, so one probe."""
    _configure(
        monkeypatch,
        active_base_url=None,
        custom_providers=[
            {"name": "Proxy A", "base_url": "https://shared.example/v1", "api_key": "k"},
            {"name": "Proxy B", "base_url": "https://shared.example/v1/", "api_key": "k"},
        ],
    )
    observed = _install_urlopen(monkeypatch, dead_hosts=[], live_hosts=["shared.example"])

    cfg.get_available_models()

    assert _hosts(observed, "live") == ["shared.example"]
