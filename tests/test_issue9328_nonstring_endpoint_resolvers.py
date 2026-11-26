"""Regression coverage for #9328, round 5 — a non-string endpoint on the
resolver paths that never went through the typed-absence rule.

The earlier rounds guarded ``custom_providers`` rows (``name``, ``model``,
``base_url``). The same hand-written scalar reaches three other readers of an
endpoint and one identity producer:

* ``_get_provider_base_url()`` read ``providers.<id>.base_url`` and the active
  ``model.base_url`` with the crashing ``(... or "").strip()`` idiom, so
  ``providers: {lmstudio: {base_url: 9}}`` and ``model: {base_url: 9}`` raised
  ``AttributeError``. It is reached unguarded by ``resolve_model_provider()``,
  by ``_resolve_model_reasoning_efforts_impl()`` (the ``/api/reasoning``
  path) and by the lmstudio readers.
* ``resolve_model_provider()`` returned the raw ``model.base_url`` value, so a
  non-string endpoint came back as an ``int``.
* The ``providers.<slug>.models`` scan stringified it, turning ``9`` into the
  endpoint ``"9"``.
* ``_named_custom_provider_slug_for_base_url()`` minted ``custom:5`` for a row
  written as ``name: 5`` — an identity the repository deliberately refuses to
  create for such a row — and that same coerced slug put the row into the
  legacy-identity set, suppressing a genuine native record for that identity.

A non-string value is ABSENT everywhere in this class: endpoint reads follow
the installed Agent's legacy-row URL reader, which accepts only ``str``, and a
non-string name names no identity. Quoted strings — including ones that need
stripping, and ``name: "5"`` — keep working exactly as before.
"""

from __future__ import annotations

import os

import pytest

import api.config as config


def _install_config(tmp_path, monkeypatch, yaml_text: str):
    """Write ``yaml_text`` to an isolated config store and load it."""
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
def providers_record_int(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  lmstudio:
    base_url: 9
""",
    )


@pytest.fixture()
def model_block_int(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
model:
  provider: lmstudio
  base_url: 9
""",
    )


@pytest.fixture()
def providers_record_valid(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  lmstudio:
    base_url: "https://ok.example/v1/"
""",
    )


@pytest.fixture()
def provider_scan_int(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
model:
  provider: openrouter
  default: gpt-base
providers:
  fireworks:
    base_url: 9
    models:
      gpt-x: {}
""",
    )


_LMSTUDIO_ACTIVE = """
model:
  provider: lmstudio
  default: local-model
providers:
  lmstudio:
    base_url: 9
"""


@pytest.fixture()
def lmstudio_int(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, _LMSTUDIO_ACTIVE)


@pytest.fixture()
def non_string_name_row(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
custom_providers:
  - name: 5
    base_url: "https://eu.example/v1"
""",
    )


@pytest.fixture()
def quoted_numeric_name_row(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
custom_providers:
  - name: "5"
    base_url: "https://eu.example/v1"
""",
    )


@pytest.fixture()
def native_record_with_junk_legacy(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  "custom:5":
    base_url: "https://native.example/v1"
    models:
      gpt-x: {}
custom_providers:
  - name: 5
    base_url: "https://legacy.example/v1"
""",
    )


@pytest.fixture()
def native_record_with_quoted_legacy(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  "custom:5":
    base_url: "https://native.example/v1"
    models:
      gpt-x: {}
custom_providers:
  - name: "5"
    base_url: "https://legacy.example/v1"
""",
    )


# ── The endpoint readers that raised ────────────────────────────────────────


def test_provider_record_endpoint_read_ignores_a_non_string_value(providers_record_int):
    """``providers.<id>.base_url: 9`` is absent, not a crash."""
    assert config._get_provider_base_url("lmstudio") is None


def test_model_block_endpoint_read_ignores_a_non_string_value(model_block_int):
    """The active ``model.base_url`` fallback applies the same rule."""
    assert config._get_provider_base_url("lmstudio") is None


def test_string_endpoint_still_strips_the_trailing_slash(providers_record_valid):
    """A quoted string keeps the historical strip — the guard is not a skip."""
    assert config._get_provider_base_url("lmstudio") == "https://ok.example/v1"


@pytest.fixture()
def unconfigured_store(tmp_path, monkeypatch):
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  lmstudio:
    api_key: x
""",
    )


def test_unconfigured_endpoint_still_returns_none(unconfigured_store):
    assert config._get_provider_base_url("lmstudio") is None


def test_reasoning_efforts_path_survives_a_non_string_endpoint(lmstudio_int):
    """``/api/reasoning`` reached the same reader unguarded."""
    assert config._resolve_model_reasoning_efforts_impl(
        model_id="local-model", provider_id="lmstudio"
    ) == []


# ── The resolver's own return value and the models scan ────────────────────


def test_resolve_never_returns_a_non_string_model_endpoint(model_block_int):
    """The raw ``model.base_url`` value must not travel back as an ``int``."""
    assert config.resolve_model_provider("gpt-x") == ("gpt-x", "lmstudio", None)


def test_provider_scan_never_stringifies_a_non_string_endpoint(provider_scan_int):
    """``providers.<slug>.models`` must not turn ``9`` into the endpoint ``"9"``."""
    assert config.resolve_model_provider("gpt-x") == ("gpt-x", "fireworks", None)


# ── Identity: a non-string name mints nothing and suppresses nothing ───────


def test_non_string_name_mints_no_named_slug(non_string_name_row):
    """The URL-to-slug fallback keeps the bare ``custom`` lane for such a row."""
    assert (
        config._named_custom_provider_slug_for_base_url("https://eu.example/v1")
        == "custom"
    )


def test_quoted_numeric_name_still_mints_its_slug(quoted_numeric_name_row):
    """A quoted ``name: "5"`` names a real identity — unchanged."""
    assert (
        config._named_custom_provider_slug_for_base_url("https://eu.example/v1")
        == "custom:5"
    )


def test_non_string_name_does_not_suppress_a_native_record(native_record_with_junk_legacy):
    """A junk row names no identity, so the native record is not shadowed."""
    entries = config._custom_provider_entries()
    assert any(e.get("base_url") == "https://native.example/v1" for e in entries)


def test_quoted_name_still_wins_over_the_native_record(native_record_with_quoted_legacy):
    """In a mixed store a real legacy identity still wins."""
    entries = config._custom_provider_entries()
    assert not any(e.get("base_url") == "https://native.example/v1" for e in entries)
