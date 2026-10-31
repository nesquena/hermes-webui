"""
Persisted-alias uniqueness regression tests for #8017 (re-gate of 7d10cdd4).

The previous fix translated a generated WebUI routing slug to the owning
row's NAME at save time, but only proved the SLUG was unique. Distinct
generated slugs can translate to the same Agent name: two rows both named
``晨光`` get different endpoint-fallback slugs (their ports differ), so
selecting the second row passed the slug check and persisted
``provider: 晨光`` — which the installed Agent resolves by FIRST name
match. The main runtime then returned the first row's endpoint and key,
and compression kept the selected endpoint but built its client with the
first row's key (maintainer's replay on PR #8031, 2026-10-22).

These tests drive the REAL setters against a real config.yaml on disk and
assert refusal (and a byte-identical file) whenever the name that would
be persisted does not resolve uniquely to the selected row, plus the
single-row / ASCII positives consumed through a first-match consumer
that mirrors the Agent's loaded-config semantics:

- main runtime: the persisted ``model.provider`` name resolves to the
  first ``custom_providers`` row carrying that name; that row's endpoint
  and key are what runs.
- compression: the auxiliary slot keeps its own persisted ``base_url``
  (the selected row's endpoint) but the client key comes from the first
  row matching the persisted provider name.
"""
import contextlib

import pytest
import yaml

import api.config as config

DUP_NAME = "晨光"
URL_FIRST = "http://127.0.0.1:8318/v1"
URL_SECOND = "http://127.0.0.1:8317/v1"
KEY_FIRST = "test-skSHADOW"
KEY_SECOND = "test-skENTRY"
MODEL = "chat-model"


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _dup_entries():
    """The maintainer's exact failing case: one name, two endpoints/keys."""
    return [
        {"name": DUP_NAME, "base_url": URL_FIRST, "api_key": KEY_FIRST, "model": MODEL},
        {"name": DUP_NAME, "base_url": URL_SECOND, "api_key": KEY_SECOND, "model": MODEL},
    ]


def _slug(entry):
    return config._custom_provider_entry_slug(entry)


# ── First-match consumers (mirror the installed Agent's semantics) ───────────


def _agent_first_match_row(config_data, persisted_provider):
    for entry in config_data.get("custom_providers", []):
        if str(entry.get("name") or "").strip() == persisted_provider:
            return entry
    return None


def _agent_main_consume(config_data):
    """(endpoint, key) the main runtime gets from the saved model block."""
    row = _agent_first_match_row(config_data, config_data["model"]["provider"])
    assert row is not None
    return row["base_url"], row["api_key"]


def _agent_compression_consume(config_data):
    """(endpoint, key) compression gets: slot endpoint, first-match key."""
    slot = config_data["auxiliary"]["compression"]
    row = _agent_first_match_row(config_data, slot["provider"])
    assert row is not None
    return slot["base_url"], row["api_key"]


# ── Disk harness ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _disk_cfg(monkeypatch, tmp_path, config_data, resolved=None):
    """Write *config_data* to a real config.yaml and point the setters at it.

    For the main setter, ``resolve_model_provider`` is stubbed to hand
    back the catalog slug of the row under selection, mirroring the
    Settings save flow; the file on disk is the oracle either way.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config_data, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    if resolved is not None:
        slug, url = resolved
        monkeypatch.setattr(
            config,
            "resolve_model_provider",
            lambda model: (model, slug, url),
        )
    yield config_path


def _main_config(entries):
    return {
        "model": {"provider": "custom", "default": "old-model"},
        "custom_providers": entries,
    }


def _aux_config(entries):
    return {
        "model": {"provider": "custom", "default": MODEL},
        "auxiliary": {"compression": {"provider": "auto", "model": ""}},
        "custom_providers": entries,
    }


# ── Duplicate names: refuse before writing either config ─────────────────────


class TestDuplicateNamePersistenceRefuses:
    def test_the_two_rows_mint_distinct_webui_slugs(self):
        entries = _dup_entries()
        assert _slug(entries[0]) != _slug(entries[1])

    def test_main_save_of_second_row_refuses_and_writes_nothing(
        self, monkeypatch, tmp_path
    ):
        entries = _dup_entries()
        slug_second = _slug(entries[1])
        with _disk_cfg(
            monkeypatch, tmp_path, _main_config(entries), (slug_second, URL_SECOND)
        ) as path:
            before = path.read_bytes()
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.set_hermes_default_model(MODEL, provider=slug_second)
            assert path.read_bytes() == before, "a refused save must write nothing"

    def test_compression_save_of_second_row_refuses_and_writes_nothing(
        self, monkeypatch, tmp_path
    ):
        entries = _dup_entries()
        slug_second = _slug(entries[1])
        with _disk_cfg(monkeypatch, tmp_path, _aux_config(entries)) as path:
            before = path.read_bytes()
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.set_auxiliary_model("compression", slug_second, MODEL)
            assert path.read_bytes() == before, "a refused save must write nothing"

    def test_main_save_of_first_row_also_refuses(self, monkeypatch, tmp_path):
        # The persisted name does not resolve UNIQUELY to either row while
        # a duplicate exists, so the first row is not a safe save either.
        entries = _dup_entries()
        slug_first = _slug(entries[0])
        with _disk_cfg(
            monkeypatch, tmp_path, _main_config(entries), (slug_first, URL_FIRST)
        ) as path:
            before = path.read_bytes()
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.set_hermes_default_model(MODEL, provider=slug_first)
            assert path.read_bytes() == before

    def test_name_claimed_via_another_rows_provider_key_refuses(
        self, monkeypatch, tmp_path
    ):
        # The Agent claims a persisted name through a row's provider_key
        # alias too, so a second row whose provider_key IS the selected
        # row's name breaks the uniqueness proof even with distinct
        # display names and distinct slugs.
        entries = [
            {"name": "别的", "provider_key": DUP_NAME, "base_url": URL_FIRST,
             "api_key": KEY_FIRST, "model": MODEL},
            {"name": DUP_NAME, "base_url": URL_SECOND,
             "api_key": KEY_SECOND, "model": MODEL},
        ]
        slug_second = _slug(entries[1])
        assert _slug(entries[0]) != slug_second
        with _disk_cfg(
            monkeypatch, tmp_path, _main_config(entries), (slug_second, URL_SECOND)
        ) as path:
            before = path.read_bytes()
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.set_hermes_default_model(MODEL, provider=slug_second)
            assert path.read_bytes() == before


# ── Positives: single row and ASCII saves still land and consume exactly ─────


class TestUniqueNamePersistencePositives:
    def test_single_row_main_save_consumes_the_selected_row(
        self, monkeypatch, tmp_path
    ):
        entry = {"name": DUP_NAME, "base_url": URL_SECOND,
                 "api_key": KEY_SECOND, "model": MODEL}
        slug = _slug(entry)
        with _disk_cfg(
            monkeypatch, tmp_path, _main_config([entry]), (slug, URL_SECOND)
        ) as path:
            result = config.set_hermes_default_model(MODEL, provider=slug)
            saved = config._load_yaml_config_file(path)
        assert result["provider"] == DUP_NAME
        assert saved["model"]["provider"] == DUP_NAME
        assert _agent_main_consume(saved) == (URL_SECOND, KEY_SECOND)

    def test_single_row_compression_save_consumes_the_selected_row(
        self, monkeypatch, tmp_path
    ):
        entry = {"name": DUP_NAME, "base_url": URL_SECOND,
                 "api_key": KEY_SECOND, "model": MODEL}
        slug = _slug(entry)
        with _disk_cfg(monkeypatch, tmp_path, _aux_config([entry])) as path:
            result = config.set_auxiliary_model("compression", slug, MODEL)
            saved = config._load_yaml_config_file(path)
        assert result["provider"] == DUP_NAME
        assert saved["auxiliary"]["compression"]["provider"] == DUP_NAME
        assert _agent_compression_consume(saved) == (URL_SECOND, KEY_SECOND)

    def test_ascii_main_save_is_byte_identical_behaviour(
        self, monkeypatch, tmp_path
    ):
        entry = {"name": "Review ASCII", "base_url": URL_SECOND,
                 "api_key": KEY_SECOND, "model": MODEL}
        slug = _slug(entry)
        assert slug == "custom:review-ascii"
        with _disk_cfg(
            monkeypatch, tmp_path, _main_config([entry]), (slug, URL_SECOND)
        ) as path:
            result = config.set_hermes_default_model(MODEL, provider=slug)
            saved = config._load_yaml_config_file(path)
        assert result["provider"] == slug
        assert saved["model"]["provider"] == slug
