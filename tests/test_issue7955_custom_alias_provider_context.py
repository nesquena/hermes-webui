"""
Regression tests for issue #7955 — ``model.provider: ollama`` with a
``base_url`` makes every Custom-group model fail with
``custom:<tag-prefix> not configured``.

The picker reports a configured provider that aliases to the generic
``custom`` lane as ``custom``, so the session stores ``custom`` while
config.yaml still says ``ollama``. ``model_with_provider_context()``
compared the two raw strings, saw a mismatch, and emitted
``@custom:qwen3.8:27b``. ``resolve_model_provider()`` then read the tag
prefix as a named-provider slug: provider ``custom:qwen3.8``, model ``27b``.

The fix marks that lane with a type, ``_ConfiguredCustomLaneModel``, which
``resolve_model_provider()`` maps to ``(model, "custom", model.base_url)``
before it looks anything up by name. Three other shapes were tried and reviewed
out:

* a bare id runs through the ``custom_providers[]`` / ``providers:`` ownership
  scans, where another endpoint listing the same id takes the request;
* the configured provider's own hint (``@ollama:<model>``) skips those scans
  but picks up a ``providers.ollama`` or ``custom_providers[name=local]``
  record of the same name, which the Custom lane never used;
* a reserved hint string (``@custom-configured:<model>``) is the same text a
  ``providers.custom-configured`` entry produces, so it took that provider's
  picks to the default endpoint and key.

``ollama -> custom`` lives in the agent's alias table
(``hermes_cli.models._PROVIDER_ALIASES``), which the WebUI merges when the
agent is importable; the tests stand in for it with a stub module holding
only that entry. ``local -> custom`` is in the WebUI's own table and needs
no agent.
"""

import sys
import types

import pytest

import api.config as config

OLLAMA_BASE_URL = "http://localhost:11434/v1"


def _set_config(
    provider, base_url=None, default=None, custom_providers=None, providers=None
):
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if default:
        model_cfg["default"] = default
    config.cfg["model"] = model_cfg
    config.cfg["providers"] = providers or {}
    config.cfg["custom_providers"] = custom_providers or []
    return old_cfg


def _restore(old_cfg):
    config.cfg.clear()
    config.cfg.update(old_cfg)


@pytest.fixture
def agent_aliases_ollama_to_custom(monkeypatch):
    """Stand in for the agent's alias table, which maps ``ollama`` to ``custom``."""
    models = types.ModuleType("hermes_cli.models")
    models._PROVIDER_ALIASES = {"ollama": "custom"}
    package = types.ModuleType("hermes_cli")
    package.models = models
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    assert config._resolve_provider_alias("ollama") == "custom"


# ── The reported bug: provider ollama + base_url, colon-tagged model ─────


def test_ollama_default_gives_the_custom_lane_its_own_hint(
    agent_aliases_ollama_to_custom,
):
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL, default="qwen3.8:27b")
    try:
        encoded = config.model_with_provider_context("qwen3.8:27b", "custom")
        assert isinstance(encoded, config._ConfiguredCustomLaneModel), (
            f"session 'custom' is the configured provider here, got {encoded!r}"
        )
        assert encoded == "qwen3.8:27b"
    finally:
        _restore(old)


def test_ollama_default_colon_tagged_model_roundtrip(agent_aliases_ollama_to_custom):
    """The tag prefix must not become a named-provider slug."""
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL, default="qwen3.8:27b")
    try:
        model, provider, base_url = config.resolve_model_provider(
            config.model_with_provider_context("qwen3.8:27b", "custom")
        )
        assert model == "qwen3.8:27b", f"tag was split off the model id: {model!r}"
        assert provider != "custom:qwen3.8", "tag prefix read as a provider slug"
        assert not str(provider).startswith("custom:"), f"got {provider!r}"
        assert base_url == OLLAMA_BASE_URL
    finally:
        _restore(old)


def test_ollama_default_non_default_colon_tagged_model_roundtrip(
    agent_aliases_ollama_to_custom,
):
    """'Every Custom-group model', not only the configured default."""
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL, default="qwen3.8:27b")
    try:
        model, provider, base_url = config.resolve_model_provider(
            config.model_with_provider_context("llama4.2:8b-instruct-q4_K_M", "custom")
        )
        assert model == "llama4.2:8b-instruct-q4_K_M"
        assert not str(provider).startswith("custom:"), f"got {provider!r}"
        assert base_url == OLLAMA_BASE_URL
    finally:
        _restore(old)


# ── Same class through the WebUI's own alias table (no agent needed) ─────


def test_legacy_local_default_colon_tagged_model_roundtrip():
    old = _set_config(provider="local", base_url=OLLAMA_BASE_URL, default="qwen3.8:27b")
    try:
        encoded = config.model_with_provider_context("qwen3.8:27b", "custom")
        assert isinstance(encoded, config._ConfiguredCustomLaneModel), f"got {encoded!r}"
        model, provider, base_url = config.resolve_model_provider(encoded)
        assert (model, provider, base_url) == ("qwen3.8:27b", "custom", OLLAMA_BASE_URL)
    finally:
        _restore(old)


# ── The configured endpoint stays authoritative over a duplicate id ──────
#
# A second endpoint ``lab`` lists the same ids. A model picked from the
# configured local lane must not move to it, for an untagged and a tagged id,
# through ``custom_providers[]`` and through ``providers:``.

LAB_BASE_URL = "http://10.0.0.8:8000/v1"
LAB_MODELS = ["mistral-7b", "qwen3.8:27b"]


def _lab(shape):
    if shape == "custom_providers":
        return {
            "custom_providers": [
                {"name": "lab", "base_url": LAB_BASE_URL, "models": list(LAB_MODELS)}
            ]
        }
    return {"providers": {"lab": {"base_url": LAB_BASE_URL, "models": list(LAB_MODELS)}}}


@pytest.mark.parametrize("shape", ["custom_providers", "providers"])
@pytest.mark.parametrize("model_id", LAB_MODELS)
def test_ollama_default_duplicate_id_keeps_configured_endpoint(
    agent_aliases_ollama_to_custom, shape, model_id
):
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL, **_lab(shape))
    try:
        resolved = config.resolve_model_provider(
            config.model_with_provider_context(model_id, "custom")
        )
        assert resolved == (model_id, "custom", OLLAMA_BASE_URL), (
            f"{model_id!r} from the Ollama lane resolved to {resolved!r}"
        )
    finally:
        _restore(old)


@pytest.mark.parametrize("shape", ["custom_providers", "providers"])
@pytest.mark.parametrize("model_id", LAB_MODELS)
def test_legacy_local_default_duplicate_id_keeps_configured_endpoint(shape, model_id):
    """``local`` is not a registered provider (#1384): the route comes back as
    ``custom``, never as ``local``."""
    old = _set_config(provider="local", base_url=OLLAMA_BASE_URL, **_lab(shape))
    try:
        resolved = config.resolve_model_provider(
            config.model_with_provider_context(model_id, "custom")
        )
        assert resolved == (model_id, "custom", OLLAMA_BASE_URL), (
            f"{model_id!r} from the local lane resolved to {resolved!r}"
        )
    finally:
        _restore(old)


def test_ollama_default_model_only_on_ollama_roundtrip(agent_aliases_ollama_to_custom):
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL, **_lab("custom_providers"))
    try:
        resolved = config.resolve_model_provider(
            config.model_with_provider_context("llama3", "custom")
        )
        assert resolved == ("llama3", "custom", OLLAMA_BASE_URL)
    finally:
        _restore(old)


# ── A record named like the configured provider is not the Custom lane ───
#
# The Custom lane is bound to ``model.base_url``. A ``providers.<name>`` or
# ``custom_providers[]`` record that happens to carry the configured
# provider's name has its own endpoint and its own key, and must not be
# picked up by a ``custom`` session, tagged or untagged.

OTHER_BASE_URL = "http://other.example:9999/v1"


def _custom_lane(model_id):
    return config.resolve_model_provider(
        config.model_with_provider_context(model_id, "custom")
    )


@pytest.mark.parametrize("model_id", ["llama3", "qwen3.8:27b"])
def test_ollama_default_ignores_a_providers_record_named_ollama(
    agent_aliases_ollama_to_custom, model_id
):
    old = _set_config(
        provider="ollama",
        base_url=OLLAMA_BASE_URL,
        providers={"ollama": {"base_url": OTHER_BASE_URL}},
    )
    try:
        assert _custom_lane(model_id) == (model_id, "custom", OLLAMA_BASE_URL)
    finally:
        _restore(old)


@pytest.mark.parametrize("model_id", ["llama3", "qwen3.8:27b"])
def test_legacy_local_default_ignores_a_providers_record_named_local(model_id):
    old = _set_config(
        provider="local",
        base_url=OLLAMA_BASE_URL,
        providers={"local": {"base_url": OTHER_BASE_URL}},
    )
    try:
        assert _custom_lane(model_id) == (model_id, "custom", OLLAMA_BASE_URL)
    finally:
        _restore(old)


@pytest.mark.parametrize("model_id", ["llama3", "qwen3.8:27b"])
def test_legacy_local_default_ignores_a_custom_provider_named_local(model_id):
    """Neither that record's endpoint nor its key: the route is the bare
    ``custom`` lane, which no named record's credential answers for."""
    old = _set_config(
        provider="local",
        base_url=OLLAMA_BASE_URL,
        custom_providers=[
            {"name": "local", "base_url": "http://named.example:7777/v1", "api_key": "sk-named"}
        ],
    )
    try:
        model, provider, base_url = _custom_lane(model_id)
        assert (model, provider, base_url) == (model_id, "custom", OLLAMA_BASE_URL)
        assert config.resolve_custom_provider_connection(provider) == (None, None)
    finally:
        _restore(old)


def test_an_explicit_pick_from_providers_local_keeps_its_own_record():
    """``@local:phi-5`` is a pick from the ``providers.local`` row, not the
    Custom lane, so it is routed by that record as before."""
    old = _set_config(
        provider="local",
        base_url=OLLAMA_BASE_URL,
        providers={"local": {"base_url": "http://explicit.example:8000/v1"}},
    )
    try:
        assert config.resolve_model_provider("@local:phi-5") == (
            "phi-5",
            "local",
            "http://explicit.example:8000/v1",
        )
    finally:
        _restore(old)


def test_an_explicit_pick_from_providers_ollama_keeps_its_own_record(
    agent_aliases_ollama_to_custom,
):
    old = _set_config(
        provider="ollama",
        base_url=OLLAMA_BASE_URL,
        providers={"ollama": {"base_url": OTHER_BASE_URL}},
    )
    try:
        assert config.resolve_model_provider("@ollama:llama3") == (
            "llama3",
            "ollama",
            OTHER_BASE_URL,
        )
    finally:
        _restore(old)


# ── Negative controls: the qualifier stays where it is still needed ──────


def test_session_on_the_configured_provider_stays_bare(agent_aliases_ollama_to_custom):
    """Existing contract: same raw provider string, bare id."""
    old = _set_config(provider="ollama", base_url=OLLAMA_BASE_URL)
    try:
        assert (
            config.model_with_provider_context("deepseek-r1:14b", "ollama")
            == "deepseek-r1:14b"
        )
    finally:
        _restore(old)


def test_custom_session_under_non_custom_default_keeps_hint():
    """A default that does not alias to custom still needs the explicit hint."""
    old = _set_config(provider="anthropic")
    try:
        encoded = config.model_with_provider_context("qwen3.8", "custom")
        assert encoded == "@custom:qwen3.8", f"got {encoded!r}"
    finally:
        _restore(old)


def test_named_custom_session_under_ollama_default_keeps_hint(
    agent_aliases_ollama_to_custom,
):
    """Only the bare 'custom' lane is the aliased default; a named one is not."""
    old = _set_config(
        provider="ollama",
        base_url=OLLAMA_BASE_URL,
        default="qwen3.8:27b",
        custom_providers=[
            {"name": "lab", "base_url": "http://lab.example:8000/v1", "model": "phi-5"}
        ],
    )
    try:
        encoded = config.model_with_provider_context("phi-5", "custom:lab")
        assert encoded == "@custom:lab:phi-5", f"got {encoded!r}"
    finally:
        _restore(old)


# ── A provider named ``custom-configured`` keeps its own endpoint and key ─
#
# The lane is carried as a type, not as text, because any ``@<name>:<model>``
# text is also what a ``providers.<name>`` entry produces. These run the real
# chain: config.yaml on disk -> the picker catalog -> the session the UI stores
# -> model_with_provider_context() -> resolve_model_provider() -> the agent's
# runtime resolver, for the URL and the credential the request would carry.

NAMED_BASE_URL = "http://127.0.0.1:18080/v1"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
_DEFAULTS = {
    "configured-custom": (
        "  provider: ollama\n"
        f"  base_url: {DEFAULT_BASE_URL}\n"
        "  api_key: review-fake-default-key\n"
    ),
    "non-custom": "  provider: openai\n  api_key: review-fake-openai-key\n",
}


def _install_config(monkeypatch, tmp_path, default):
    import yaml

    text = (
        "model:\n"
        f"{_DEFAULTS[default]}"
        "  default: qwen3.8:27b\n"
        "providers:\n"
        "  custom-configured:\n"
        f"    base_url: {NAMED_BASE_URL}\n"
        "    api_key: review-fake-named-key\n"
        "    models: [qwen3.8:27b, plainmodel]\n"
    )
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    old = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(yaml.safe_load(text))
    config.invalidate_models_cache()
    return old


def _route(session_model, session_provider):
    """What a send with this session state reaches: the resolved triple, then
    the runtime's URL and credential for that provider."""
    runtime_provider = pytest.importorskip("hermes_cli.runtime_provider")
    from api.oauth import resolve_runtime_provider_with_anthropic_env_lock

    model, provider, base_url = config.resolve_model_provider(
        config.model_with_provider_context(session_model, session_provider)
    )
    runtime = resolve_runtime_provider_with_anthropic_env_lock(
        runtime_provider.resolve_runtime_provider, requested=provider
    )
    return model, provider, base_url, runtime.get("base_url"), runtime.get("api_key")


@pytest.mark.parametrize("default", sorted(_DEFAULTS))
@pytest.mark.parametrize("model_id", ["qwen3.8:27b", "plainmodel"])
def test_a_provider_named_custom_configured_keeps_its_endpoint_and_key(
    monkeypatch, tmp_path, default, model_id
):
    old = _install_config(monkeypatch, tmp_path, default)
    try:
        groups = config.get_available_models()["groups"]
        group = next(g for g in groups if g.get("provider_id") == "custom-configured")
        (option,) = [m["id"] for m in group["models"] if m["id"].endswith(model_id)]
        # The session the UI stores for that pick: the option id and its group.
        route = _route(option, group["provider_id"])
    finally:
        _restore(old)
        config.invalidate_models_cache()
    assert route == (
        model_id,
        "custom-configured",
        NAMED_BASE_URL,
        NAMED_BASE_URL,
        "review-fake-named-key",
    )


@pytest.mark.parametrize("model_id", ["qwen3.8:27b", "plainmodel"])
def test_the_configured_custom_lane_beside_it_keeps_the_default_endpoint_and_key(
    monkeypatch, tmp_path, model_id
):
    old = _install_config(monkeypatch, tmp_path, "configured-custom")
    try:
        route = _route(model_id, "custom")
    finally:
        _restore(old)
        config.invalidate_models_cache()
    assert route == (
        model_id,
        "custom",
        DEFAULT_BASE_URL,
        DEFAULT_BASE_URL,
        "review-fake-default-key",
    )
