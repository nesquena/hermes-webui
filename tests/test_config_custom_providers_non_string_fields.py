"""Regression coverage for #9328 — legacy ``custom_providers`` rows whose
``name`` / ``model`` / ``base_url`` is not a string.

Legacy rows are hand-written YAML, so ``name: 5`` (or ``model: 7``,
``base_url: 9``) reaches ``resolve_model_provider()`` as an ``int`` and the
inline ``(... or '').strip()`` reads raised
``AttributeError: 'int' object has no attribute 'strip'`` — taking down the
whole resolution, including the ``@<provider>:`` and default-model save paths
that only touch those rows in passing.

The resolver must treat a non-string field as ABSENT (the Agent's own
legacy-row reader skips such rows) instead of coercing it with ``str()`` —
coercion would mint a provider literally named "5", a row the user never wrote.
"""

from __future__ import annotations

import os

import pytest
import yaml

import api.config as config


# A legacy store mixing bad and good rows: non-string ``name``, non-string
# ``model``, non-string ``base_url``, then one well-formed row that must keep
# resolving exactly as before.
LEGACY_YAML = """
model:
  provider: openrouter
  default: gpt-x
custom_providers:
  - name: 5
    base_url: "https://eu-proxy.example/v1"
  - model: 7
  - name: eu
    base_url: 9
  - name: eu-proxy
    base_url: "https://eu-proxy.example/v1"
    models:
      gpt-x: {}
      eu-valid: {}
"""

# Active provider IS the named custom entry, so the resolver reaches
# ``_entry_owns_model()`` and the ``_active_owner`` base_url read as well.
ACTIVE_LEGACY_YAML = """
model:
  provider: "custom:eu"
  default: gpt-x
custom_providers:
  - model: 7
  - name: eu
    base_url: 9
    models:
      eu-only: {}
"""


# Non-OpenRouter active provider, so the slash-form cross-provider scan (which
# reads `name` and then `base_url` of the matching row) is actually reached.
# Row 1 has a non-string `name`, row 2 matches the `eu/` prefix with a
# non-string `base_url`: reverting either guard turns this call into the same
# AttributeError.
SLASH_LEGACY_YAML = """
model:
  provider: anthropic
  default: claude-base
custom_providers:
  - name: 5
    base_url: "https://ignored.example/v1"
  - name: eu
    base_url: 9
  - name: proxy
    base_url: "https://proxy.example/v1"
"""


def _install_config(tmp_path, monkeypatch, yaml_text: str) -> "os.PathLike[str]":
    """Write ``yaml_text`` to an isolated config.yaml and load it into ``cfg``.

    The original ``HERMES_CONFIG_PATH`` is restored (and the module's ``cfg``
    reloaded) on teardown so the next test never inherits this store.
    """
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    previous = os.environ.get("HERMES_CONFIG_PATH")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    config.reload_config()
    yield path
    if previous is None:
        os.environ.pop("HERMES_CONFIG_PATH", None)
    else:
        os.environ["HERMES_CONFIG_PATH"] = previous
    config.reload_config()


@pytest.fixture()
def legacy_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, LEGACY_YAML)


@pytest.fixture()
def active_custom_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, ACTIVE_LEGACY_YAML)


@pytest.fixture()
def slash_legacy_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, SLASH_LEGACY_YAML)


def test_bare_model_resolves_with_non_string_legacy_rows(legacy_store):
    """The configured default still resolves past the malformed rows."""
    assert config.resolve_model_provider("gpt-x") == ("gpt-x", "openrouter", None)


def test_provider_qualified_hint_does_not_raise(legacy_store):
    """``@<built-in>:`` hints cross the ownership scan before being parsed."""
    for model_id in ("@openrouter:gpt-x", "@custom:gpt-x"):
        resolved = config.resolve_model_provider(model_id)
        assert isinstance(resolved, tuple) and len(resolved) == 3


def test_set_hermes_default_model_does_not_raise(legacy_store):
    """The v0.52.742 save path resolves the model with an ``@<provider>:`` hint."""
    result = config.set_hermes_default_model("gpt-x", provider="openrouter:eu")
    assert result["ok"] is True
    assert result["model"] == "gpt-x"


def test_valid_row_still_resolves_with_its_base_url(legacy_store):
    """A well-formed row keeps its slug and endpoint, unchanged by the guard."""
    assert config.resolve_model_provider("eu-valid") == (
        "eu-valid",
        "custom:eu-proxy",
        "https://eu-proxy.example/v1",
    )
    assert legacy_store.exists()


def test_active_custom_provider_with_non_string_base_url(active_custom_store):
    """The active-slug branch reads ``base_url`` through the same guard."""
    assert config.resolve_model_provider("eu-only") == ("eu-only", "custom:eu", None)


def test_slash_form_prefix_scan_with_non_string_fields(slash_legacy_store):
    """Slash-form routing skips non-string ``name`` and ``base_url`` alike.

    The active provider is ``anthropic``, so ``eu/model`` falls through to the
    cross-provider custom-name scan: row ``{name: 5}`` must not crash the
    ``name`` comparison, and the matching ``eu`` row's integer ``base_url`` must
    be treated as absent rather than raising.
    """
    assert config.resolve_model_provider("eu/model") == (
        "eu/model",
        "custom:eu",
        None,
    )


def test_yaml_source_is_really_a_legacy_store(legacy_store):
    """Guard the fixture itself: the malformed rows must survive YAML parsing."""
    parsed = yaml.safe_load(legacy_store.read_text(encoding="utf-8"))
    rows = parsed["custom_providers"]
    assert rows[0]["name"] == 5
    assert rows[1]["model"] == 7
    assert rows[2]["base_url"] == 9
