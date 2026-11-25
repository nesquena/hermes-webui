"""Regression coverage for #8954 — a switched-off ``providers.<slug>`` record
must not route a model or supply a base_url.

The bug: ``resolve_model_provider``'s ``providers.<slug>.models`` scan
(``api/config.py``) returned the first record whose ``models`` list the target
with that record's slug spelling and ``base_url``, without ever reading
``enabled``. With ``model.provider: openai`` and a switched-off ``OpenAI``
spelling listing the session model, the chat's connection bundle became
"enabled record's key (KX) at the disabled record's URL (b.example)" — KX's
own source declares only a.example. The Agent authenticates through the
enabled record (it lowercases the requested id and its disabled check reads
``providers.openai``), so key and URL came from two different records.

The fix (maintainer-specified): skip switched-off records in that scan using
the module's existing ``_raw_provider_record_enabled`` helper, so the chat
routes as if the record didn't list the model. A switched-off record
shouldn't route a model or supply a URL.

Pins (all covered below):
  * the issue's shape in both dict orders, with only the disabled spelling
    listing the model or both spellings listing it;
  * a disabled exact-key record listing the model (the Agent refuses; the
    WebUI no longer hands out that record's URL either);
  * an enabled other spelling listing the model (routes to its own URL,
    unchanged).

Plus regression controls: ``enabled`` default / ``true`` / string forms keep
routing unchanged; the copilot exception, the ``custom:`` key skip and the
active-provider ownership guard are not bypassed; and a differential control
proving the scan change is surgical — over a batch of randomly generated
providers configs, the resolver output is byte-identical before/after the
fix unless the differing record was explicitly switched off.
"""
from __future__ import annotations

import copy
import random

import api.config as config

# Fake hosts only — never a real endpoint (see repo security rules).
URL_B = "https://b.example/v1"
URL_A = "https://a.example/v1"
URL_R = "https://r.example/v1"
URL_C = "https://c.example/v1"


def _cfg(providers: dict, *, provider: str = "openai", default=None) -> dict:
    model_block = {"provider": provider}
    if default is not None:
        model_block["default"] = default
    return {"model": model_block, "providers": providers}


def _resolve(providers: dict, model_id: str = "m-x", monkeypatch=None, **cfg_kwargs) -> tuple:
    """Drive ``resolve_model_provider`` over a throwaway in-memory config."""
    cfg = _cfg(copy.deepcopy(providers), **cfg_kwargs)
    if monkeypatch is not None:
        monkeypatch.setattr(config, "cfg", cfg, raising=False)
    else:  # pragma: no cover - probes only
        config.cfg = cfg
    return config.resolve_model_provider(model_id)


def _resolve_cfg(cfg: dict, model_id: str = "m-x", monkeypatch=None) -> tuple:
    """Same, for a full config dict (model block included)."""
    if monkeypatch is not None:
        monkeypatch.setattr(config, "cfg", copy.deepcopy(cfg), raising=False)
    else:  # pragma: no cover - probes only
        config.cfg = cfg
    return config.resolve_model_provider(model_id)


def _resolve_scan_without_enabled(cfg: dict, model_id: str, monkeypatch) -> tuple:
    """Same config, but the scan's enabled check pinned to always-true.

    With the fix in place this reproduces the pre-fix scan behaviour exactly
    (the only ``_raw_provider_record_enabled`` call site reachable on this
    path is the scan's); it is the "before" side of the differential control.
    The original helper is restored before returning so the caller's "after"
    run sees the real check.
    """
    original = config._raw_provider_record_enabled
    monkeypatch.setattr(config, "_raw_provider_record_enabled", lambda record: True)
    try:
        return _resolve_cfg(cfg, model_id, monkeypatch)
    finally:
        monkeypatch.setattr(config, "_raw_provider_record_enabled", original)


def _disabled_record_explains(before: tuple, after: tuple, providers: dict, model_id: str) -> bool:
    """True when a before/after difference is explained by one switched-off record.

    The pre-fix scan was a first-match over the same traversal, so a difference
    is legitimate only when ``before`` handed back a record that is explicitly
    switched off — its slug spelling and its own base_url.
    """
    if before == after:
        return False
    for slug, pdef in providers.items():
        if not isinstance(pdef, dict):
            continue
        if model_id not in config._configured_model_ids(pdef.get("models")):
            continue
        if config._raw_provider_record_enabled(pdef):
            continue
        url = str(pdef.get("base_url") or "").strip() or None
        if before == (model_id, slug, url):
            return True
    return False


# ── Pin 1: the issue's shape ──────────────────────────────────────────────


def test_disabled_spelling_only_lister_does_not_route_disabled_first(monkeypatch):
    """Only the switched-off spelling lists the model, and it comes first.

    Pre-fix the scan returned the switched-off record: ('m-x', 'OpenAI',
    b.example) — the enabled record's key at the disabled record's URL.
    Post-fix the scan skips it, the enabled record doesn't list the model, and
    the resolver falls back to the active provider.
    """
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
            "openai": {"base_url": URL_A, "api_key": "KX"},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", None), (
        "a switched-off providers.<slug> record must not route the model or "
        f"supply its base_url; got {result!r}"
    )


def test_disabled_spelling_only_lister_does_not_route_enabled_first(monkeypatch):
    """Same shape, opposite dict order — config order must not matter."""
    result = _resolve(
        {
            "openai": {"base_url": URL_A, "api_key": "KX"},
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", None), (
        "a switched-off providers.<slug> record must not route the model or "
        f"supply its base_url; got {result!r}"
    )


def test_both_spellings_list_model_disabled_first_routes_to_enabled(monkeypatch):
    """Both spellings list the model, disabled first: route to the enabled one."""
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
            "openai": {"base_url": URL_A, "api_key": "KX", "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", URL_A), (
        "the enabled spelling must win when the switched-off one lists the "
        f"model first; got {result!r}"
    )


def test_both_spellings_list_model_enabled_first_unchanged(monkeypatch):
    """Control: enabled spelling first — the documented-correct behaviour."""
    result = _resolve(
        {
            "openai": {"base_url": URL_A, "api_key": "KX", "models": {"m-x": {}}},
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", URL_A)


def test_neither_spelling_lists_model_unchanged(monkeypatch):
    """Control: nothing lists the model — fallback to the active provider."""
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False},
            "openai": {"base_url": URL_A, "api_key": "KX"},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", None)


# ── Pin 2: a disabled exact-key record listing the model ──────────────────


def test_disabled_exact_key_record_no_longer_supplies_url(monkeypatch):
    """The session provider's own record is switched off and lists the model.

    The Agent refuses this record anyway (its disabled check reads
    ``providers.<active>``); the WebUI must not hand its base_url out either.
    """
    result = _resolve(
        {
            "openai": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", None), (
        "a switched-off exact-key record must not supply a URL to the chat "
        f"bundle; got {result!r}"
    )


# ── Pin 3: an enabled other spelling listing the model ────────────────────


def test_enabled_other_spelling_still_routes_to_its_own_url(monkeypatch):
    """Control: enabled (default) other spelling — routes to its own URL."""
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "OpenAI", URL_B)


def test_enabled_true_other_spelling_still_routes(monkeypatch):
    """Control: explicit ``enabled: true`` — routes unchanged."""
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": True, "models": {"m-x": {}}},
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "OpenAI", URL_B)


# ── ``enabled`` spellings: only falsey words hide the record ──────────────


def test_string_enabled_false_words_do_not_route(monkeypatch):
    """YAML may hand ``enabled`` as a string; the disabled words still hide."""
    for word in ("false", "FALSE", " off ", "no", "0"):
        result = _resolve(
            {
                "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": word, "models": {"m-x": {}}},
                "openai": {"base_url": URL_A, "api_key": "KX"},
            },
            monkeypatch=monkeypatch,
        )
        assert result == ("m-x", "openai", None), (
            f"enabled: {word!r} must hide the record; got {result!r}"
        )


def test_string_enabled_truthy_words_still_route(monkeypatch):
    """Non-disabled strings are enabled — routing unchanged."""
    for word in ("true", "yes", "on", "1"):
        result = _resolve(
            {
                "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": word, "models": {"m-x": {}}},
            },
            monkeypatch=monkeypatch,
        )
        assert result == ("m-x", "OpenAI", URL_B), (
            f"enabled: {word!r} must keep the record routable; got {result!r}"
        )


# ── Branches that must not be bypassed ────────────────────────────────────


def test_copilot_settings_map_never_routes_enabled_or_disabled(monkeypatch):
    """``providers.copilot.models`` is a per-model SETTINGS map, not a route."""
    for flag in (None, False, True):
        record = {"base_url": URL_C, "api_key": "K3", "models": {"m-x": {}}}
        if flag is not None:
            record["enabled"] = flag
        result = _resolve({"copilot": record}, monkeypatch=monkeypatch)
        assert result == ("m-x", "openai", None), (
            f"copilot record (enabled={flag!r}) must not be routed by the "
            f"scan; got {result!r}"
        )


def test_custom_key_records_still_go_through_the_read_view(monkeypatch):
    """``custom:`` keys are skipped by the scan and left to the read view."""
    result = _resolve(
        {"custom:relay": {"base_url": URL_R, "api_key": "K4", "models": {"m-x": {}}}},
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "custom:relay", URL_R), (
        f"the custom-key branch must be untouched; got {result!r}"
    )


def test_switched_off_custom_key_record_fails_closed(monkeypatch):
    """A switched-off custom-key record is invisible to the read view too."""
    result = _resolve(
        {
            "custom:relay": {
                "base_url": URL_R, "api_key": "K4", "enabled": False, "models": {"m-x": {}},
            },
        },
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", None), (
        f"a switched-off custom-key record must fail closed; got {result!r}"
    )


def test_active_provider_ownership_guard_not_bypassed(monkeypatch):
    """When the active provider owns the model, only its own slug may match.

    The guard compares canonical slugs, so the switched-off ``OpenAI`` spelling
    still counts as the active provider's own record — the fix must skip it
    and land on the enabled same-canonical record, never on an unrelated slug.
    """
    result = _resolve(
        {
            "OpenAI": {"base_url": URL_B, "api_key": "K2", "enabled": False, "models": {"m-x": {}}},
            "openai": {"base_url": URL_A, "api_key": "KX", "models": {"m-x": {}}},
            "anthropic": {"base_url": URL_C, "api_key": "K5", "models": {"m-x": {}}},
        },
        default="m-x",
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", URL_A), (
        "the ownership guard must keep routing on the active provider's own "
        f"records; got {result!r}"
    )


def test_ownership_guard_blocks_unrelated_slug_when_active_owns(monkeypatch):
    """Guard control: an unrelated enabled slug never hijacks an owned model."""
    result = _resolve(
        {
            "anthropic": {"base_url": URL_C, "api_key": "K5", "models": {"m-x": {}}},
            "openai": {"base_url": URL_A, "api_key": "KX", "models": {"m-x": {}}},
        },
        default="m-x",
        monkeypatch=monkeypatch,
    )
    assert result == ("m-x", "openai", URL_A), (
        f"an unrelated slug must not hijack an active-owned model; got {result!r}"
    )


# ── Differential control: the change is surgical ──────────────────────────

_DIFF_SLUGS = ("openai", "OpenAI", "OPENAI", "anthropic", "Anthropic", "copilot", "Copilot")
_DIFF_URLS = ("https://a.example/v1", "https://b.example/v1", "https://c.example/v1")
_DIFF_ENABLED = (None, True, False, "true", "false", "off", "no", "yes", "0", "1")
_DIFF_MODEL_SHAPES = (
    {"m-x": {}},
    {"m-x": {}, "m-other": {}},
    ["m-x"],
    ["m-other"],
    None,
)


def _random_providers_configs(count: int = 240, seed: int = 8954) -> list[dict]:
    rng = random.Random(seed)
    configs = []
    for _ in range(count):
        n = rng.randint(1, 4)
        slugs = rng.sample(_DIFF_SLUGS, n)
        providers: dict = {}
        for slug in slugs:
            record: dict = {
                "base_url": rng.choice(_DIFF_URLS),
                "api_key": "K" + slug.lower(),
            }
            models = rng.choice(_DIFF_MODEL_SHAPES)
            if models is not None:
                record["models"] = copy.deepcopy(models)
            enabled = rng.choice(_DIFF_ENABLED)
            if enabled is not None:
                record["enabled"] = enabled
            providers[slug] = record
        provider = rng.choice(("openai", "anthropic"))
        default = rng.choice((None, "m-x", "m-other"))
        configs.append(_cfg(providers, provider=provider, default=default))
    return configs


def test_differential_control_only_switched_off_records_change(monkeypatch):
    """Before/after the fix the resolver output is byte-identical, unless the
    difference is a record the user explicitly switched off.

    The "before" side pins the scan's enabled check to always-true, which on
    this path (bare model id, built-in slugs, no custom_providers) reproduces
    the pre-fix scan exactly — the scan's own check is the only reachable
    ``_raw_provider_record_enabled`` call site.
    """
    diffs = 0
    for cfg in _random_providers_configs():
        providers = cfg["providers"]
        before = _resolve_scan_without_enabled(cfg, "m-x", monkeypatch)
        after = _resolve_cfg(cfg, "m-x", monkeypatch)
        if before != after:
            diffs += 1
            assert _disabled_record_explains(before, after, providers, "m-x"), (
                f"resolver output changed without a switched-off record "
                f"explaining it: before={before!r} after={after!r} cfg={providers!r}"
            )
        # ...and the fixed scan must never hand back a switched-off record.
        for slug, pdef in providers.items():
            if not isinstance(pdef, dict):
                continue
            if config._raw_provider_record_enabled(pdef):
                continue
            url = str(pdef.get("base_url") or "").strip() or None
            assert after != ("m-x", slug, url), (
                f"the fixed scan routed to switched-off record {slug!r}: {after!r}"
            )
    assert diffs > 0, (
        "differential control observed no differences — the random batch "
        "failed to exercise the switched-off shape"
    )
    print(f"differential control: 240 configs, {diffs} differing (all switched-off records)")
