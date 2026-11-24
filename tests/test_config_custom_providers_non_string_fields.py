"""Regression coverage for #9328 — legacy ``custom_providers`` rows whose
``name`` / ``model`` / ``base_url`` is not a string.

Legacy rows are hand-written YAML, so ``name: 5`` (or ``model: 7``,
``base_url: 9``) reaches ``resolve_model_provider()`` as an ``int`` and the
inline ``(... or '').strip()`` reads raised
``AttributeError: 'int' object has no attribute 'strip'`` — taking down the
whole resolution, including the ``@<provider>:`` and default-model save paths
that only touch those rows in passing.

The resolver must treat a non-string field as ABSENT instead of coercing it
with ``str()`` — coercion would mint a provider literally named "5", a row the
user never wrote. Endpoint reads follow the installed Agent's legacy-row URL
reader (``hermes_cli.config_providers._pick_provider_base_url``), which accepts
only ``str``; identity reads are deliberately stricter here than that reader's
``coerce_provider_id``, which does coerce a name.
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


# ── Typed absence must survive the ENDPOINT and IDENTITY consumers too ───────
#
# The resolver-only guard above cleared the crash, but a non-string field still
# reached three consumers that coerced it into valid-looking text:
#
#   * the named ``@custom:<slug>:`` explicit-hint endpoint read;
#   * the connection view / save paths (default model and auxiliary slot), which
#     persisted the coerced endpoint into the store;
#   * the legacy identity producers, where ``name: 5`` minted the provider
#     ``custom:5`` — a row the user never wrote.
#
# A non-string endpoint is ABSENT (the Agent's own legacy-row URL reader,
# ``hermes_cli.config_providers._pick_provider_base_url``, only accepts
# ``str``), and a non-string ``name`` names no identity at all. Genuinely quoted
# string names — including ones that need stripping — keep working unchanged.

# One well-formed row and one whose endpoint is an ``int``. The valid row is
# padded to prove the string path still strips instead of being skipped.
NAMED_ENDPOINT_YAML = """
model:
  provider: openrouter
  default: gpt-x
custom_providers:
  - name: eu
    base_url: 9
    models:
      gpt-x: {}
  - name: eu-ok
    base_url: " https://eu-ok.example/v1 "
    models:
      gpt-x: {}
"""

# A row whose ``name`` is an ``int`` next to an ordinary valid row.
MANUFACTURED_NAME_YAML = """
model:
  provider: openrouter
  default: gpt-x
custom_providers:
  - name: 5
    base_url: "https://five.example/v1"
    models:
      gpt-x: {}
  - name: eu-ok
    base_url: "https://eu-ok.example/v1"
    models:
      gpt-x: {}
"""

# The non-string ``name`` must not collide with a genuinely quoted ``"5"`` row:
# collision detection runs on real identities only, so the quoted row wins
# instead of the pair raising ``AmbiguousCustomProviderError``.
QUOTED_FIVE_YAML = """
model:
  provider: openrouter
  default: gpt-x
custom_providers:
  - name: 5
    base_url: 9
  - name: "5"
    base_url: "https://five-quoted.example/v1"
"""


@pytest.fixture()
def named_endpoint_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, NAMED_ENDPOINT_YAML)


@pytest.fixture()
def manufactured_name_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, MANUFACTURED_NAME_YAML)


@pytest.fixture()
def quoted_five_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, QUOTED_FIVE_YAML)


def _saved_model(path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("model") or {}


def test_named_hint_endpoint_treats_non_string_base_url_as_absent(named_endpoint_store):
    """``@custom:<slug>:<model>`` must not hand back ``"9"`` as an endpoint."""
    assert config.resolve_model_provider("@custom:eu:gpt-x") == ("gpt-x", "custom:eu", None)
    # Control: an ordinary string endpoint is still stripped and returned.
    assert config.resolve_model_provider("@custom:eu-ok:gpt-x") == (
        "gpt-x",
        "custom:eu-ok",
        "https://eu-ok.example/v1",
    )


def test_connection_view_treats_non_string_base_url_as_absent(named_endpoint_store):
    """The connection consumer reads the same typed absence as the resolver."""
    assert config.resolve_custom_provider_connection("custom:eu") == (None, None)
    assert config.resolve_custom_provider_connection("custom:eu-ok") == (
        None,
        "https://eu-ok.example/v1",
    )


def test_default_model_save_does_not_persist_coerced_endpoint(named_endpoint_store):
    """Saving a named selection must not write ``base_url: "9"`` (and reloading
    must not reproduce it)."""
    result = config.set_hermes_default_model("@custom:eu:gpt-x", provider="custom:eu")
    assert result["ok"] is True
    saved = _saved_model(named_endpoint_store)
    assert "9" != str(saved.get("base_url") or "")
    assert "base_url" not in saved
    config.reload_config()
    assert config.resolve_model_provider("@custom:eu:gpt-x")[2] is None
    # Control: the valid row's endpoint is still persisted verbatim.
    result = config.set_hermes_default_model("@custom:eu-ok:gpt-x", provider="custom:eu-ok")
    assert result["ok"] is True
    assert _saved_model(named_endpoint_store)["base_url"] == "https://eu-ok.example/v1"


def test_auxiliary_save_does_not_persist_coerced_endpoint(named_endpoint_store):
    """The auxiliary slot reads the selected provider's own entry — typed too."""
    result = config.set_auxiliary_model("vision", "custom:eu", "gpt-x")
    assert result["ok"] is True
    saved = (yaml.safe_load(named_endpoint_store.read_text(encoding="utf-8")) or {})
    slot = (saved.get("auxiliary") or {}).get("vision") or {}
    assert "base_url" not in slot
    # Control: the valid row still persists its endpoint on the slot.
    result = config.set_auxiliary_model("vision", "custom:eu-ok", "gpt-x")
    assert result["ok"] is True
    saved = (yaml.safe_load(named_endpoint_store.read_text(encoding="utf-8")) or {})
    assert (saved.get("auxiliary") or {})["vision"]["base_url"] == "https://eu-ok.example/v1"


def test_non_string_name_row_names_no_identity(manufactured_name_store):
    """``name: 5`` must not mint the provider ``custom:5``."""
    assert "custom:5" not in config._named_custom_provider_slugs()
    assert config.resolve_custom_provider_connection("custom:5") == (None, None)
    assert config.resolve_model_provider("@custom:5:gpt-x") == ("gpt-x", "custom:5", None)
    # The save/readback half: nothing of that row reaches the store.
    result = config.set_hermes_default_model("@custom:5:gpt-x", provider="custom:5")
    assert result["ok"] is True
    saved = _saved_model(manufactured_name_store)
    assert "base_url" not in saved
    # Control: the ordinary row is still an identity with its own endpoint.
    assert config.resolve_custom_provider_connection("custom:eu-ok") == (
        None,
        "https://eu-ok.example/v1",
    )


def test_non_string_name_does_not_collide_with_quoted_name(quoted_five_store):
    """Identity validation runs before the collision check.

    With ``name: 5`` (no identity) beside ``name: "5"`` (a real one), the quoted
    row is the single owner of ``custom:5`` — the pair must not raise
    ``AmbiguousCustomProviderError``, and its endpoint is the quoted row's.
    """
    assert config.resolve_custom_provider_connection("custom:5") == (
        None,
        "https://five-quoted.example/v1",
    )



def test_yaml_source_is_really_a_legacy_store(legacy_store):
    """Guard the fixture itself: the malformed rows must survive YAML parsing."""
    parsed = yaml.safe_load(legacy_store.read_text(encoding="utf-8"))
    rows = parsed["custom_providers"]
    assert rows[0]["name"] == 5
    assert rows[1]["model"] == 7
    assert rows[2]["base_url"] == 9
