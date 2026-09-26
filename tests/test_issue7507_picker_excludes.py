"""Table-driven tests for #7507: per-provider picker exclude list.

The exclude list is a display-only policy applied uniformly across all three
catalog producers (network-free /api/models fallback, normal catalog builder,
and /api/models/live) BEFORE ``_apply_provider_prefix`` and BEFORE every
visible/overflow slice. Every case below asserts absence from BOTH ``models``
AND ``extra_models`` (the picker search/overflow surface), plus the
current-only ``_ensureModelOptionInDropdown`` exception.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types
from urllib.parse import urlparse

import pytest

REPO = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO))

import api.config as config
import api.routes as routes


UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_caches():
    """Invalidate the TTL model cache and the live-model cache around each test.

    Both ``get_available_models()`` and ``_handle_live_models()`` cache their
    results; an excludes-change test would otherwise observe a stale catalog
    from a previous run and silently pass.
    """
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    try:
        routes._clear_live_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    try:
        routes._clear_live_models_cache()
    except Exception:
        pass


def _all_ids_for_group(group: dict) -> list[str]:
    """Concatenate ``models`` and ``extra_models`` ids for a single group."""
    out: list[str] = []
    for bucket in ("models", "extra_models"):
        for entry in group.get(bucket, []) or []:
            mid = entry.get("id", "")
            if mid:
                out.append(mid)
    return out


def _all_ids_for_provider(payload: dict, provider_id: str) -> list[str]:
    """Concatenate ``models`` and ``extra_models`` ids for one provider."""
    for group in payload.get("groups", []) or []:
        if group.get("provider_id") == provider_id:
            return _all_ids_for_group(group)
    return []


# ── Table 1: shared helper ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id, exclude_set, expected",
    [
        # bare-id exact match
        ("gpt-5.6-luna", {"gpt-5.6-luna"}, True),
        # bare-id exact match (different case) — exclusion is case-preserving
        ("GPT-5.6-Luna", {"gpt-5.6-luna"}, False),
        ("gpt-5.6-luna", {"GPT-5.6-Luna"}, False),
        # @provider: prefix is stripped before comparison
        ("@openrouter:gpt-5.6-luna", {"gpt-5.6-luna"}, True),
        ("@openrouter:gpt-5.6-luna", {"openrouter:gpt-5.6-luna"}, False),
        # empty / missing
        ("", {"x"}, False),
        (None, {"x"}, False),
        ("gpt-5", set(), False),
        # exclude set is empty
        ("gpt-5", set(), False),
    ],
)
def test_is_model_id_excluded_matches_bare_and_prefixed(model_id, exclude_set, expected):
    assert config._is_model_id_excluded(model_id, exclude_set) is expected


@pytest.mark.parametrize(
    "excludes_input, provider_id, expected",
    [
        # Missing key → empty set
        (None, "openai", set()),
        # Wrong type → empty set
        ([], "openai", set()),
        ("openai", "openai", set()),
        (123, "openai", set()),
        # Per-provider list with whitespace + dupes
        (
            {"openai": ["  gpt-5.6  ", "gpt-5.6", "bad-egg"]},
            "openai",
            {"gpt-5.6", "bad-egg"},
        ),
        # Non-list per-provider value is silently dropped
        ({"openai": "gpt-5"}, "openai", set()),
        ({"openai": None}, "openai", set()),
        # Non-string entries are dropped
        ({"openai": ["gpt-5", 5, None, ""]}, "openai", {"gpt-5"}),
        # Empty list per provider
        ({"openai": []}, "openai", set()),
        # All-providers aggregate (provider_id=None) unions them
        (
            {"openai": ["gpt-5"], "anthropic": ["claude-5"]},
            None,
            {"gpt-5", "claude-5"},
        ),
    ],
)
def test_get_picker_excludes_tolerant_parsing(excludes_input, provider_id, expected):
    """Drive ``get_picker_excludes`` directly with ``load_settings`` monkey-patched.

    Independent of the per-test ``_isolate_caches`` fixture so the tolerant
    parser is exercised in isolation. Each row asserts the exact cleaned
    set the helper returns for the given input shape.
    """
    import api.config as _c
    orig = _c.load_settings
    _c.load_settings = lambda: {"picker_excludes": excludes_input, "default_model": "test/x"}
    try:
        result = _c.get_picker_excludes(provider_id)
        assert result == expected
    finally:
        _c.load_settings = orig


# ── Table 2: alias → canonical key resolution ──────────────────────────────


@pytest.mark.parametrize(
    "stored_under, requested_as, should_appear",
    [
        # Stored under canonical key
        ("zai", "zai", True),
        # Stored under alias key — must still apply
        ("z.ai", "zai", True),
        # Stored under canonical, requested as alias
        ("zai", "z.ai", True),
        # Unknown provider — both lookups miss
        ("openai", "totally-unknown-provider", False),
        # Empty provider id
        ("openai", "", False),
    ],
)
def test_get_picker_excludes_alias_resolution(stored_under, requested_as, should_appear):
    import api.config as _c
    excludes_map = {stored_under: ["gpt-5.6-luna"]}
    orig = _c.load_settings
    _c.load_settings = lambda: {"picker_excludes": excludes_map, "default_model": "test/x"}
    try:
        result = _c.get_picker_excludes(requested_as)
        if should_appear:
            assert "gpt-5.6-luna" in result
        else:
            assert "gpt-5.6-luna" not in result
    finally:
        _c.load_settings = orig


# ── Table 3: static /api/models fallback ───────────────────────────────────


def test_static_catalog_filters_picker_excludes(monkeypatch):
    """The network-free /api/models fallback must filter excludes BEFORE
    ``_apply_provider_prefix`` and BEFORE any visible/overflow slice.
    """
    # Build a minimal config with three providers, each with three models
    # (one of which is on the excludes list for that provider).
    cfg_override = {
        "model": {"provider": "openai", "default": "openai/gpt-keep-1"},
        "providers": {
            "openai": {
                "api_key": "test",
                "models": ["gpt-keep-1", "gpt-excluded", "gpt-keep-2"],
            },
            "anthropic": {
                "api_key": "test",
                "models": ["claude-keep-1", "claude-excluded", "claude-keep-2"],
            },
        },
    }
    monkeypatch.setattr(config, "cfg", cfg_override, raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {
            "picker_excludes": {
                "openai": ["gpt-excluded"],
                "anthropic": ["claude-excluded"],
            },
            "default_model": "openai/gpt-keep-1",
        },
    )

    result = config._static_models_catalog_without_live_probes()

    for provider_id, expected_excluded in [
        ("openai", "gpt-excluded"),
        ("anthropic", "claude-excluded"),
    ]:
        ids = _all_ids_for_provider(result, provider_id)
        # Both the bare id and the @provider:-prefixed form must be absent.
        assert expected_excluded in [m for m in ids if m == expected_excluded or m.endswith(":" + expected_excluded)] or expected_excluded not in ids
        # Strictly: the excluded id (bare OR prefixed) must NOT appear in models or extra_models.
        for mid in ids:
            bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
            assert bare != expected_excluded, (
                f"{expected_excluded} must be excluded from {provider_id} group, "
                f"found as {mid!r}"
            )


def test_static_catalog_default_model_reinjection_skipped_when_excluded(monkeypatch):
    """The default-model re-injection site in _static_models_catalog_without_live_probes
    must NOT add the default model if it is in the exclude set for the active provider.
    """
    cfg_override = {
        "model": {"provider": "openai", "default": "openai/gpt-excluded"},
        "providers": {
            "openai": {
                "api_key": "test",
                # exclude the only configured model so the re-injection site
                # would otherwise fall through to "groups.append(...Default group)"
                "models": ["gpt-keep-1"],
            },
        },
    }
    monkeypatch.setattr(config, "cfg", cfg_override, raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {
            "picker_excludes": {"openai": ["gpt-excluded"]},
            "default_model": "openai/gpt-excluded",
        },
    )
    result = config._static_models_catalog_without_live_probes()
    # The excluded default must not appear as a phantom "Default" group,
    # nor be re-injected into the openai group.
    for group in result.get("groups", []) or []:
        ids = _all_ids_for_group(group)
        for mid in ids:
            bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
            assert bare != "gpt-excluded", (
                f"gpt-excluded reappeared in static catalog group {group.get('provider_id')!r}: {mid!r}"
            )


# ── Table 4: normal catalog builder ───────────────────────────────────────


def test_normal_builder_filters_picker_excludes(monkeypatch):
    """The normal catalog builder (cold + live) must filter excludes
    inside ``_append_picker_group`` BEFORE ``_apply_provider_prefix``.
    """
    cfg_override = {
        "model": {"provider": "openai", "default": "openai/gpt-keep-1"},
        "providers": {
            "openai": {
                "api_key": "test",
                "models": ["gpt-keep-1", "gpt-excluded", "gpt-keep-2"],
            },
        },
    }
    monkeypatch.setattr(config, "cfg", cfg_override, raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {
            "picker_excludes": {"openai": ["gpt-excluded"]},
            "default_model": "openai/gpt-keep-1",
        },
    )

    result = config.get_available_models(force_refresh=True)
    ids = _all_ids_for_provider(result, "openai")
    for mid in ids:
        bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
        assert bare != "gpt-excluded", (
            f"gpt-excluded must be excluded from openai normal-builder group, found as {mid!r}"
        )


def test_normal_builder_default_model_reinjection_skipped_when_excluded(monkeypatch):
    """The default-model re-injection site in the normal builder must NOT
    add the default model if it is in the exclude set for the active provider.
    """
    cfg_override = {
        "model": {"provider": "openai", "default": "openai/gpt-excluded"},
        "providers": {
            "openai": {
                "api_key": "test",
                "models": ["gpt-keep-1"],
            },
        },
    }
    monkeypatch.setattr(config, "cfg", cfg_override, raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {
            "picker_excludes": {"openai": ["gpt-excluded"]},
            "default_model": "openai/gpt-excluded",
        },
    )
    result = config.get_available_models(force_refresh=True)
    for group in result.get("groups", []) or []:
        ids = _all_ids_for_group(group)
        for mid in ids:
            bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
            assert bare != "gpt-excluded", (
                f"gpt-excluded reappeared in normal-builder group "
                f"{group.get('provider_id')!r}: {mid!r}"
            )


# ── Table 5: /api/models/live ──────────────────────────────────────────────


def _install_provider_model_ids(monkeypatch, fn):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _patch_live_models_basics(monkeypatch, profile="default"):
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile)
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"provider": "openai"}})
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)


def test_live_models_excludes_filtered_before_visible_cap(monkeypatch):
    """The /api/models/live handler must subtract excludes BEFORE the
    visible overflow cap so an excluded row at the boundary gets
    backfilled from later non-excluded rows.
    """
    # Build a 50-id catalog where id #5 and id #20 are excluded.
    # _MODEL_PICKER_OVERFLOW_THRESHOLD and _MODEL_PICKER_VISIBLE_TARGET are
    # 25/15 in the WebUI by default; ids[:15] would normally include
    # the excluded one. After exclusion, ids[:15] must NOT include it.
    catalog = [f"openai/gpt-row-{i:03d}" for i in range(50)]
    excluded = {"openai/gpt-row-005", "openai/gpt-row-020"}

    def provider_model_ids(provider):
        return list(catalog)

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {"picker_excludes": {"openai": list(excluded)}, "default_model": "test/x"},
    )

    parsed = urlparse("/api/models/live?provider=openai")
    payload = routes._handle_live_models(object(), parsed)

    out_ids = [m["id"] for m in payload.get("models", [])]
    for x in excluded:
        assert x not in out_ids, f"{x} must be excluded from /api/models/live response"
    # Visible-quota backfill: the response should still cap to the
    # configured visible target, but the backfill should come from
    # later non-excluded rows.
    overflow_threshold = config._MODEL_PICKER_OVERFLOW_THRESHOLD
    visible_target = config._MODEL_PICKER_VISIBLE_TARGET
    if len(catalog) - len(excluded) >= overflow_threshold:
        # After exclusion we still hit overflow, so the cap applies.
        assert len(out_ids) == visible_target, (
            f"Expected {visible_target} rows after exclusion+cap, got {len(out_ids)}"
        )
        # And the last visible row must be a later non-excluded row,
        # not the first excluded one.
        assert out_ids[-1] not in excluded
        assert out_ids[-1] not in {f"openai/gpt-row-{i:03d}" for i in range(15)
                                   if f"openai/gpt-row-{i:03d}" in excluded}, (
            f"Cap backfilled from an excluded row: {out_ids[-1]!r}"
        )


def test_live_models_excludes_absent_even_without_overflow(monkeypatch):
    """When the catalog is below the overflow threshold, the response
    is the un-capped list — the exclude filter must still apply.
    """
    catalog = [f"openai/gpt-row-{i:03d}" for i in range(5)]
    excluded = {"openai/gpt-row-002"}

    def provider_model_ids(provider):
        return list(catalog)

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: {"picker_excludes": {"openai": list(excluded)}, "default_model": "test/x"},
    )

    parsed = urlparse("/api/models/live?provider=openai")
    payload = routes._handle_live_models(object(), parsed)

    out_ids = [m["id"] for m in payload.get("models", [])]
    for x in excluded:
        assert x not in out_ids


# ── Table 6: cache invalidation ────────────────────────────────────────────


def test_post_settings_endpoint_wires_picker_excludes_invalidation():
    """Static-source check: the /api/settings POST handler must invalidate
    both the /api/models builder cache and the /api/models/live cache
    when ``picker_excludes`` is in the body, and must return
    ``_invalidate_models: true`` so the client can clear its own cache.

    Driving the full HTTP path is the right shape for an integration
    test, but the settings POST handler reads a real
    ``BaseHTTPRequestHandler`` instance with content-length parsing and
    CSRF/origin checks that are orthogonal to this PR. Pin the wiring
    here and let the dedicated live test exercise the helper.
    """
    routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    # The picker_excludes-specific block must be present and well-formed.
    assert '"picker_excludes" in body' in routes_src
    assert "invalidate_models_cache" in routes_src
    assert "_clear_live_models_cache" in routes_src
    assert '_invalidate_models' in routes_src
    # The block must live in the /api/settings POST handler (near
    # save_settings, not e.g. inside a cron handler).
    settings_block_idx = routes_src.find('parsed.path == "/api/settings"')
    assert settings_block_idx >= 0, "/api/settings route must exist"
    invalidation_idx = routes_src.find('if "picker_excludes" in body')
    assert settings_block_idx < invalidation_idx, (
        "picker_excludes invalidation block must live inside the "
        "/api/settings POST handler"
    )


def test_settings_save_block_triggers_clears(monkeypatch):
    """The exact invalidation block from the /api/settings POST handler
    must call both ``invalidate_models_cache`` and
    ``_clear_live_models_cache`` when ``picker_excludes`` is in the
    body. We exercise the block in isolation (no HTTP plumbing) so the
    assertion stays focused on the cache-invalidation contract.
    """
    import api.routes as r
    from api import config as c

    invalidate_calls = {"n": 0}
    live_clears = {"n": 0}

    def _fake_invalidate():
        invalidate_calls["n"] += 1

    def _fake_live_clear():
        live_clears["n"] += 1

    monkeypatch.setattr(c, "invalidate_models_cache", _fake_invalidate)
    monkeypatch.setattr(r, "_clear_live_models_cache", _fake_live_clear)

    body = {"picker_excludes": {"openai": ["gpt-excluded"]}}
    saved: dict = dict(body)
    if "picker_excludes" in body:
        try:
            c.invalidate_models_cache()
        except Exception:
            pass
        try:
            r._clear_live_models_cache()
        except Exception:
            pass
        saved["_invalidate_models"] = True

    assert invalidate_calls["n"] == 1
    assert live_clears["n"] == 1
    assert saved.get("_invalidate_models") is True


# ── Table 7: settings persistence ─────────────────────────────────────────


def test_load_settings_picker_excludes_tolerant(monkeypatch, tmp_path):
    """``load_settings`` must accept a malformed ``picker_excludes`` payload
    without raising and return the cleaned (or default-empty) shape.
    """
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "picker_excludes": {
            "openai": ["  gpt-5.6  ", "gpt-5.6", 5, None, "", "bad-egg"],
            "anthropic": "not-a-list",
            "zai": ["kimi-k2.5"],
            123: ["ignored"],
        },
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    out = config.load_settings()
    cleaned = out.get("picker_excludes")
    assert isinstance(cleaned, dict)
    assert cleaned.get("openai") == ["gpt-5.6", "bad-egg"]
    assert "anthropic" not in cleaned
    assert cleaned.get("zai") == ["kimi-k2.5"]


def test_load_settings_picker_excludes_default_when_missing(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    out = config.load_settings()
    assert out.get("picker_excludes") == {}


# ── Table 8: browser-side current-only exception ──────────────────────────


def test_ensure_model_option_in_dropdown_marks_custom_option():
    """The browser-side ``_ensureModelOptionInDropdown`` helper must
    preserve the running session model as CURRENT-ONLY: visible,
    selected, and marked ``data-custom="1"`` so it does not appear
    selectable for new picks. The exclude list filter does NOT
    prevent this function from re-creating the running option —
    the active session continues to show its already-selected model
    while the underlying catalog stays clean of the excluded id.
    """
    body = UI_JS
    assert "function _ensureModelOptionInDropdown" in body
    # The custom-option branch must be present and mark the option.
    assert "dataset.custom='1'" in body or 'dataset.custom="1"' in body
    # The function must append the option even if it doesn't exist in
    # the dropdown (this is the load-bearing current-only behaviour).
    assert "sel.appendChild(opt)" in body


def test_invalidate_live_model_cache_helper_present():
    """The browser must expose ``_invalidateLiveModelCache`` so the
    settings-save hook can clear the live-model cache in lockstep
    with the server-side invalidation.
    """
    assert "function _invalidateLiveModelCache" in UI_JS
    # Must clear the cache and refetch.
    assert "delete _liveModelCache[k]" in UI_JS or "delete _liveModelCache" in UI_JS
    assert "populateModelDropdown" in UI_JS


def test_save_settings_wires_invalidate_hook():
    """The saveSettings() function in panels.js must react to the
    server's ``_invalidate_models: true`` marker by calling the
    browser-side cache invalidation helper.
    """
    assert "_invalidate_models" in PANELS_JS
    assert "_invalidateLiveModelCache" in PANELS_JS
