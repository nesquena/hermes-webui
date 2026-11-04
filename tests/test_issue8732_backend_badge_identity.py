"""Regression tests for #8732: backend configured-model badges.

Follow-up to #8725 / #8730, which fixed the frontend lookup. The backend badge
builders had the same two defects:

1. When the configured id was not an exact option key, the fallback compared ids
   by first-slash-segment-stripped identity over the visible models only. Same-
   provider siblings such as ``any/gpt-6.1-sol`` and ``codex/gpt-6.1-sol``
   collapsed to one key, so the badge landed on the first sibling in list order,
   and a configured id past the visible cut (in ``extra_models``) never matched.
2. ``fallback_providers`` entries were resolved with ``_resolve_provider_alias``,
   so a legacy ``custom_providers`` entry named ``magpie`` produced badge
   provider ``magpie`` while its picker group is ``custom:magpie``.

The shape below is the one pinned in the issue: a legacy custom provider
``magpie`` serving 34 ids with ``codex/gpt-6.1-sol`` listed last, and
``fallback_providers: [{provider: magpie, model: codex/gpt-6.1-sol}]``.

The review of #8741 added two shapes, covered at the end of this file:

- a fallback entry whose ``base_url`` ``urlparse`` rejects must not fail the
  whole ``/api/models`` response (the handler calls ``get_available_models()``
  with no guard, so a raise there is an HTTP 500);
- a bare configured id (``claude-sonnet-4``) on a relay that lists it under one
  vendor spelling (``anthropic/claude-sonnet-4``) keeps its badge on that row,
  because the frontend dedupes the bare key against that row and would
  otherwise show neither a badge nor a "Configured" row.

The re-review found the other direction regressing: a configured
``anthropic/claude-sonnet-4`` on a relay that lists only ``claude-sonnet-4``
badged the bare row, and the frontend does not dedupe the configured key
against that row, so the picker showed two "Configured" choices that save
different ids. That direction now badges no row, so the picker keeps the one
choice that saves the configured id under its own provider (as on master). The
last section runs the production ``renderModelDropdown()`` under a fake DOM on
this backend's live and static payloads and asserts how many "Configured"
choices it lays out and what clicking each of them saves.

The re-gate found the deeper shape duplicating the same way: a configured
``deepseek/deepseek-v4-pro`` (or a bare ``deepseek-v4-pro``) on a relay that
lists ``vendor_a/deepseek/deepseek-v4-pro`` badged that row, but the picker
strips only one leading segment (#3360) and does not dedupe the configured key
against it. The vendor-spelling match is now limited to exactly
``<vendor>/<bare id>``, the one shape the picker dedupes, so those rows stay
unbadged and the picker keeps the one choice that saves the configured id.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import api.config as config
from tests.js_source_extract import extract_function

TARGET = "codex/gpt-6.1-sol"
SIBLINGS = ["any/gpt-6.1-sol", "auto/gpt-6.1-sol"]
# 34 ids: the siblings first (inside the visible cut), the configured id last
# (past the cut, so it lives in ``extra_models``).
MAGPIE_IDS = SIBLINGS + [f"vendor{i:02d}/model-{i:02d}" for i in range(31)] + [TARGET]

MAGPIE = {"name": "magpie", "base_url": "https://magpie.example/v1", "api_key": "magpie-key"}
RELAY = {"name": "relay", "base_url": "https://relay.example/v1", "api_key": "relay-key"}
# The review's entry: ``urlparse("http://[bad")`` raises ``ValueError: Invalid IPv6 URL``.
MALFORMED_FALLBACK = {"provider": "custom", "base_url": "http://[bad", "model": "gpt-4o"}

UI_JS = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _live_models(monkeypatch, tmp_path, *, custom_providers, fallback_providers, served, model=None) -> dict:
    """Live ``get_available_models()`` with each custom provider served from a local mock.

    ``served`` maps a mock host (``magpie.example``) to the ids its ``/v1/models`` lists.
    ``model`` is the primary ``model:`` config; the default keeps it off the mocked providers.
    """

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout=10):
        url = getattr(req, "full_url", str(req))
        for host, model_ids in served.items():
            if host in url:
                return FakeResponse({"data": [{"id": model_id} for model_id in model_ids]})
        raise urllib.error.URLError(f"no network in this test: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Pin the synchronous live rebuild: this file tests the live badge builder,
    # not the budget fallback (which uses the static builder, tested below).
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    for name, value in (
        ("cfg", {
            "model": model or {"provider": "openai-codex", "default": "gpt-5.4"},
            "custom_providers": custom_providers,
            "fallback_providers": fallback_providers,
            "providers": {},
        }),
        ("_cfg_mtime", 0.0),
        ("_available_models_cache", None),
        ("_available_models_cache_ts", 0.0),
        ("_available_models_live_rebuild_ts", 0.0),
        ("_available_models_cache_source_fingerprint", None),
    ):
        monkeypatch.setattr(config, name, value)
    return config.get_available_models()


def _available_models(monkeypatch, tmp_path, fallback_provider: str, model_ids: list[str]) -> dict:
    """The issue's shape: legacy ``magpie`` serving *model_ids*, one fallback entry on it."""
    return _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[MAGPIE],
        fallback_providers=[{"provider": fallback_provider, "model": TARGET}],
        served={"magpie.example": model_ids},
    )


def _group(result: dict, provider_id: str) -> dict:
    groups = [g for g in result.get("groups", []) if g.get("provider_id") == provider_id]
    assert len(groups) == 1, [g.get("provider_id") for g in result.get("groups", [])]
    return groups[0]


def _magpie_group(result: dict) -> dict:
    return _group(result, "custom:magpie")


def _option_ids(group: dict) -> list[str]:
    return [m["id"] for bucket in ("models", "extra_models") for m in group.get(bucket, [])]


def _fallback_keys(result: dict) -> set[str]:
    return {k for k, v in result["configured_model_badges"].items() if v.get("role") == "fallback"}


def _assert_badge_on_target_only(result: dict) -> None:
    group = _magpie_group(result)
    ids = _option_ids(group)
    target_id = next(i for i in ids if i.endswith(TARGET))
    badges = result["configured_model_badges"]
    assert badges.get(target_id, {}).get("label") == "Fallback 1", sorted(_fallback_keys(result))
    assert badges[target_id]["provider"] == "custom:magpie"
    for sibling in SIBLINGS:
        sibling_id = next(i for i in ids if i.endswith(sibling))
        assert sibling_id not in badges, f"{sibling_id} got the configured model's badge"


@pytest.mark.parametrize("fallback_provider", ["magpie", "custom:magpie"])
def test_fallback_badge_lands_on_configured_id_past_the_visible_cut(monkeypatch, tmp_path, fallback_provider):
    result = _available_models(monkeypatch, tmp_path, fallback_provider, MAGPIE_IDS)
    group = _magpie_group(result)
    # The issue's shape: the configured id is listed last, past the visible cut.
    assert not any(m["id"].endswith(TARGET) for m in group["models"])
    assert any(m["id"].endswith(TARGET) for m in group.get("extra_models", []))
    _assert_badge_on_target_only(result)


@pytest.mark.parametrize("fallback_provider", ["magpie", "custom:magpie"])
def test_fallback_badge_lands_on_last_visible_id_not_first_sibling(monkeypatch, tmp_path, fallback_provider):
    # Short list: everything is visible and the configured id is still last.
    short = SIBLINGS + ["vendor00/model-00", TARGET]
    result = _available_models(monkeypatch, tmp_path, fallback_provider, short)
    assert not _magpie_group(result).get("extra_models")
    _assert_badge_on_target_only(result)


def test_legacy_custom_provider_name_badges_use_the_picker_group_spelling(monkeypatch, tmp_path):
    result = _available_models(monkeypatch, tmp_path, "magpie", MAGPIE_IDS)
    fallback = [v for v in result["configured_model_badges"].values() if v.get("role") == "fallback"]
    assert fallback and {b["provider"] for b in fallback} == {"custom:magpie"}
    # No synthesized ``@magpie:...`` key that no picker row can carry.
    assert not any(k.startswith("@magpie:") for k in result["configured_model_badges"])


def test_configured_id_missing_from_provider_list_badges_no_sibling(monkeypatch, tmp_path):
    # The configured model is not served at all: no sibling may borrow its badge.
    result = _available_models(monkeypatch, tmp_path, "custom:magpie", SIBLINGS + ["vendor00/model-00"])
    ids = _option_ids(_magpie_group(result))
    badges = result["configured_model_badges"]
    assert not any(i in badges for i in ids), sorted(_fallback_keys(result))


def test_static_catalog_badges_match_the_live_builder():
    groups = [
        {
            "provider": "magpie",
            "provider_id": "custom:magpie",
            "models": [{"id": f"@custom:magpie:{i}"} for i in MAGPIE_IDS[:15]],
            "extra_models": [{"id": f"@custom:magpie:{i}"} for i in MAGPIE_IDS[15:]],
        }
    ]
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(
        {
            "custom_providers": [{"name": "magpie", "base_url": "http://magpie.invalid:8880/v1"}],
            "fallback_providers": [{"provider": "magpie", "model": TARGET}],
        }
    )
    try:
        badges = config._configured_model_badges_from_static_catalog(
            groups, active_provider=None, default_model=""
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert badges.get(f"@custom:magpie:{TARGET}", {}).get("provider") == "custom:magpie"
    for sibling in SIBLINGS:
        assert f"@custom:magpie:{sibling}" not in badges


def _static_badges(
    groups: list[dict],
    fallback_providers: list[dict],
    custom_providers: list[dict] | None = None,
    *,
    active_provider: str | None = None,
    default_model: str = "",
) -> dict:
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({"fallback_providers": fallback_providers, "custom_providers": custom_providers or []})
    try:
        return config._configured_model_badges_from_static_catalog(
            groups, active_provider=active_provider, default_model=default_model
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def test_vendor_namespace_stays_part_of_the_identity():
    # ``azure/gpt-4o`` and ``openai/gpt-4o`` on one relay are different models.
    # The configured spelling differs in case, so only the identity match can
    # find it, and it must not fall on the first vendor sibling.
    groups = [
        {
            "provider": "relay",
            "provider_id": "custom:relay",
            "models": [{"id": "@custom:relay:azure/gpt-4o"}, {"id": "@custom:relay:openai/gpt-4o"}],
        }
    ]
    badges = _static_badges(groups, [{"provider": "custom:relay", "model": "OpenAI/GPT-4o"}])
    assert badges.get("@custom:relay:openai/gpt-4o", {}).get("label") == "Fallback 1"
    assert "@custom:relay:azure/gpt-4o" not in badges


def test_openrouter_identity_keeps_dash_and_dot_routes_apart():
    # docs/architecture/model-picker-routing.md: OpenRouter routes that differ
    # only by dash/dot spelling are distinct and must not be folded together.
    groups = [
        {
            "provider": "OpenRouter",
            "provider_id": "openrouter",
            "models": [{"id": "vendor/model-4.5"}, {"id": "vendor/model-4-5"}],
        }
    ]
    badges = _static_badges(groups, [{"provider": "openrouter", "model": "Vendor/Model-4-5"}])
    assert badges.get("vendor/model-4-5", {}).get("label") == "Fallback 1"
    assert "vendor/model-4.5" not in badges


# ---------------------------------------------------------------------------
# Review of #8741, point 1: a malformed fallback entry must not fail /api/models.
# ---------------------------------------------------------------------------


def _relay_groups(model_ids: list[str]) -> list[dict]:
    return [
        {
            "provider": "relay",
            "provider_id": "custom:relay",
            "models": [{"id": f"@custom:relay:{i}"} for i in model_ids],
        }
    ]


def test_malformed_fallback_base_url_falls_back_to_the_plain_provider_alias():
    # The base-url match is what raises; the badge helper must swallow it and
    # keep the entry's plain alias, which is what the builders used before #8732.
    with pytest.raises(ValueError):
        config._normalize_base_url_for_match(MALFORMED_FALLBACK["base_url"])
    assert config._configured_badge_fallback_provider(MALFORMED_FALLBACK, {"custom_providers": [RELAY]}) == "custom"


def test_malformed_fallback_base_url_keeps_the_static_badges():
    badges = _static_badges(
        _relay_groups(["claude-sonnet-4", "openai/gpt-4o"]),
        [MALFORMED_FALLBACK, {"provider": "relay", "model": "claude-sonnet-4"}],
        custom_providers=[RELAY],
    )
    # The malformed entry still badges, under its plain alias ...
    assert badges.get("gpt-4o") == {"role": "fallback", "label": "Fallback 1", "provider": "custom"}
    # ... and the entries after it are unaffected.
    assert badges.get("@custom:relay:claude-sonnet-4") == {"role": "fallback", "label": "Fallback 2", "provider": "custom:relay"}


def test_malformed_fallback_base_url_does_not_fail_get_available_models(monkeypatch, tmp_path):
    # /api/models calls get_available_models() with no guard (api/routes.py), so
    # a ValueError out of the badge builder is the HTTP 500 the review reproduced.
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=[MALFORMED_FALLBACK, {"provider": "relay", "model": "claude-sonnet-4"}],
        served={"relay.example": ["claude-sonnet-4", "openai/gpt-4o"]},
    )
    ids = _option_ids(_group(result, "custom:relay"))
    badges = result["configured_model_badges"]
    assert badges.get("gpt-4o", {}).get("provider") == "custom"
    configured_id = next(i for i in ids if i.endswith("claude-sonnet-4"))
    assert badges.get(configured_id) == {"role": "fallback", "label": "Fallback 2", "provider": "custom:relay"}


# ---------------------------------------------------------------------------
# Review of #8741, point 2: a bare configured id keeps its badge on the one row
# that spells it with a vendor prefix. The frontend dedupes the bare key against
# that row (static/ui.js _isEquivalentConfiguredModelEntry), so without a row
# key the picker showed neither a badge nor a "Configured" row.
# ---------------------------------------------------------------------------

BARE_FALLBACK = [{"provider": "relay", "model": "claude-sonnet-4"}]


def test_bare_configured_id_badges_its_single_vendor_spelling():
    badges = _static_badges(_relay_groups(["anthropic/claude-sonnet-4", "openai/gpt-4o"]), BARE_FALLBACK, custom_providers=[RELAY])
    assert badges.get("@custom:relay:anthropic/claude-sonnet-4") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}
    assert "@custom:relay:openai/gpt-4o" not in badges


REVERSE_FALLBACK = [{"provider": "relay", "model": "anthropic/claude-sonnet-4"}]


def test_vendor_qualified_configured_id_leaves_the_bare_row_unbadged():
    # The other direction: config says ``anthropic/claude-sonnet-4``, the relay
    # lists the bare id. Nothing proves the relay routes both strings to one
    # model, and the frontend does not dedupe the configured key against the
    # bare row (re-review of #8741: badging it showed two "Configured" choices
    # that save different ids). Only the configured spellings stay keyed, so the
    # picker synthesizes the one choice that saves the configured id.
    badges = _static_badges(_relay_groups(["claude-sonnet-4", "openai/gpt-4o"]), REVERSE_FALLBACK, custom_providers=[RELAY])
    assert "@custom:relay:claude-sonnet-4" not in badges
    assert "@custom:relay:openai/gpt-4o" not in badges
    assert badges.get("anthropic/claude-sonnet-4") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}
    assert badges.get("@custom:relay:anthropic/claude-sonnet-4") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}


def test_live_builder_leaves_the_bare_row_unbadged_too(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=REVERSE_FALLBACK,
        served={"relay.example": ["claude-sonnet-4", "openai/gpt-4o"]},
    )
    ids = _option_ids(_group(result, "custom:relay"))
    badges = result["configured_model_badges"]
    bare_row = next(i for i in ids if i.endswith(":claude-sonnet-4"))
    assert bare_row not in badges
    assert badges.get("anthropic/claude-sonnet-4", {}).get("provider") == "custom:relay"


def test_exact_identity_beats_a_vendor_spelling():
    badges = _static_badges(_relay_groups(["anthropic/claude-sonnet-4", "claude-sonnet-4"]), BARE_FALLBACK, custom_providers=[RELAY])
    assert badges.get("@custom:relay:claude-sonnet-4", {}).get("label") == "Fallback 1"
    assert "@custom:relay:anthropic/claude-sonnet-4" not in badges


def test_two_vendor_spellings_of_a_bare_id_badge_neither():
    # Ambiguous: the config does not say which vendor it means, so no row is
    # guessed from list order (that guess was the #8732 defect).
    badges = _static_badges(_relay_groups(["anthropic/claude-sonnet-4", "bedrock/claude-sonnet-4"]), BARE_FALLBACK, custom_providers=[RELAY])
    assert "@custom:relay:anthropic/claude-sonnet-4" not in badges
    assert "@custom:relay:bedrock/claude-sonnet-4" not in badges
    # The configured spelling itself stays keyed for the frontend.
    assert badges.get("claude-sonnet-4", {}).get("provider") == "custom:relay"


def test_single_same_provider_sibling_still_gets_no_badge():
    # The vendor-spelling match never crosses vendor namespaces: ``codex/x``
    # configured and only ``any/x`` served is still a sibling, not a spelling.
    groups = [{"provider": "magpie", "provider_id": "custom:magpie", "models": [{"id": "@custom:magpie:any/gpt-6.1-sol"}]}]
    badges = _static_badges(groups, [{"provider": "magpie", "model": TARGET}], custom_providers=[MAGPIE])
    assert "@custom:magpie:any/gpt-6.1-sol" not in badges


DEEPER_FALLBACK = [{"provider": "relay", "model": "deepseek/deepseek-v4-pro"}]
BARE_DEEPER_FALLBACK = [{"provider": "relay", "model": "deepseek-v4-pro"}]
DEEPER_SERVED = ["vendor_a/deepseek/deepseek-v4-pro", "openai/gpt-4o"]


def test_deeper_vendor_spelling_badges_no_row():
    # Re-gate of #8741: ``deepseek/deepseek-v4-pro`` configured and only
    # ``vendor_a/deepseek/deepseek-v4-pro`` served. The picker strips one
    # leading segment (#3360), so it does not dedupe the configured key against
    # that row; badging the row laid out two "Configured" choices that saved
    # different ids. No row is badged: ``vendor_b/deepseek/deepseek-v4`` is a
    # different model and ``vendor_a/deepseek-v4-pro`` a sibling. The configured
    # spellings stay keyed, so the picker synthesizes the one choice.
    badges = _static_badges(
        _relay_groups(["vendor_a/deepseek/deepseek-v4-pro", "vendor_b/deepseek/deepseek-v4", "vendor_a/deepseek-v4-pro"]),
        DEEPER_FALLBACK,
        custom_providers=[RELAY],
    )
    assert "@custom:relay:vendor_a/deepseek/deepseek-v4-pro" not in badges
    assert "@custom:relay:vendor_b/deepseek/deepseek-v4" not in badges
    assert "@custom:relay:vendor_a/deepseek-v4-pro" not in badges
    assert badges.get("deepseek/deepseek-v4-pro") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}
    assert badges.get("@custom:relay:deepseek/deepseek-v4-pro") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}


def test_bare_configured_id_does_not_badge_a_deeper_vendor_row():
    # The same picker limit from the other side: a bare ``deepseek-v4-pro`` is
    # deduped against ``<vendor>/deepseek-v4-pro`` only, never against
    # ``vendor_a/deepseek/deepseek-v4-pro``.
    badges = _static_badges(_relay_groups(DEEPER_SERVED), BARE_DEEPER_FALLBACK, custom_providers=[RELAY])
    assert "@custom:relay:vendor_a/deepseek/deepseek-v4-pro" not in badges
    assert "@custom:relay:openai/gpt-4o" not in badges
    assert badges.get("deepseek-v4-pro") == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}


def test_live_builder_badges_no_row_for_the_deeper_vendor_spelling(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=DEEPER_FALLBACK,
        served={"relay.example": DEEPER_SERVED},
    )
    ids = _option_ids(_group(result, "custom:relay"))
    badges = result["configured_model_badges"]
    deeper_row = next(i for i in ids if i.endswith("vendor_a/deepseek/deepseek-v4-pro"))
    assert deeper_row not in badges
    assert badges.get("deepseek/deepseek-v4-pro", {}).get("provider") == "custom:relay"


def test_openrouter_bare_id_does_not_badge_a_vendor_route():
    # docs/architecture/model-picker-routing.md: an OpenRouter route matches
    # exactly; no vendor path is split to find it.
    groups = [{"provider": "OpenRouter", "provider_id": "openrouter", "models": [{"id": "openai/gpt-4o"}]}]
    badges = _static_badges(groups, [{"provider": "openrouter", "model": "gpt-4o"}])
    assert "openai/gpt-4o" not in badges


def test_live_builder_badges_the_single_vendor_spelling_too(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=BARE_FALLBACK,
        served={"relay.example": ["anthropic/claude-sonnet-4", "openai/gpt-4o"]},
    )
    ids = _option_ids(_group(result, "custom:relay"))
    badges = result["configured_model_badges"]
    vendor_row = next(i for i in ids if i.endswith("anthropic/claude-sonnet-4"))
    other_row = next(i for i in ids if i.endswith("openai/gpt-4o"))
    assert badges.get(vendor_row) == {"role": "fallback", "label": "Fallback 1", "provider": "custom:relay"}
    assert other_row not in badges


# The primary entry (``model: {provider: relay, default: claude-sonnet-4}``) goes
# through the same match, so the review's shape holds for it as well.


def test_bare_primary_model_badges_its_single_vendor_spelling():
    badges = _static_badges(
        _relay_groups(["anthropic/claude-sonnet-4", "openai/gpt-4o"]),
        [],
        custom_providers=[RELAY],
        active_provider="custom:relay",
        default_model="claude-sonnet-4",
    )
    assert badges.get("@custom:relay:anthropic/claude-sonnet-4") == {"role": "primary", "label": "Primary", "provider": "custom:relay"}
    assert "@custom:relay:openai/gpt-4o" not in badges


def test_live_builder_badges_the_primary_single_vendor_spelling_too(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=[],
        served={"relay.example": ["anthropic/claude-sonnet-4", "openai/gpt-4o"]},
        model={"provider": "relay", "default": "claude-sonnet-4"},
    )
    ids = _option_ids(_group(result, "custom:relay"))
    badges = result["configured_model_badges"]
    vendor_row = next(i for i in ids if i.endswith("anthropic/claude-sonnet-4"))
    other_row = next(i for i in ids if i.endswith("openai/gpt-4o"))
    assert badges.get(vendor_row) == {"role": "primary", "label": "Primary", "provider": "custom:relay"}
    assert other_row not in badges


# The combined tree: this backend's badge map through the real frontend helpers
# (#8730's provider-scoped badge lookup and the dedupe that decides whether a
# badge key becomes a synthesized "Configured" row), composed the way
# renderModelDropdown() does it in static/ui.js.
_PICKER_DRIVER = r"""
var window={};
__SOURCE__
const [badgeMap, rows] = JSON.parse(process.argv[1]);
const modelData = rows.map(r => ({
  value: r.value,
  providerId: r.providerId,
  badge: _getConfiguredModelBadge(r.value, badgeMap, r.providerId),
}));
const synthesized = [];
for (const [modelId, badge] of Object.entries(badgeMap)) {
  if (_isEquivalentConfiguredModelEntry(modelId, badge, modelData)) continue;
  synthesized.push(modelId);
}
process.stdout.write(JSON.stringify({
  badged: modelData.filter(m => m.badge).map(m => [m.value, m.badge.label]),
  synthesized,
}));
"""


def _picker_rows(badge_map: dict, rows: list[dict]) -> dict:
    source = "\n".join(
        extract_function(UI_JS, name)
        for name in (
            "_normalizeConfiguredModelKey",
            "_configuredModelIdentity",
            "_isEquivalentConfiguredModelEntry",
            "_getConfiguredModelBadge",
        )
    )
    result = subprocess.run(
        [NODE, "-e", _PICKER_DRIVER.replace("__SOURCE__", source), json.dumps([badge_map, rows])],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_combined_tree_bare_configured_id_shows_one_badged_row_and_no_extra_row():
    # Before this change: no row key from the backend, and the frontend deduped
    # the bare key against the vendor row, so the picker showed no badge and no
    # "Configured" row for a configured model the relay does serve.
    groups = _relay_groups(["anthropic/claude-sonnet-4", "openai/gpt-4o"])
    badges = _static_badges(groups, BARE_FALLBACK, custom_providers=[RELAY])
    rows = [{"value": m["id"], "providerId": "custom:relay"} for m in groups[0]["models"]]
    picker = _picker_rows(badges, rows)
    assert picker["badged"] == [["@custom:relay:anthropic/claude-sonnet-4", "Fallback 1"]]
    assert picker["synthesized"] == []


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_combined_tree_sibling_shape_badges_only_the_configured_id():
    groups = [
        {
            "provider": "magpie",
            "provider_id": "custom:magpie",
            "models": [{"id": f"@custom:magpie:{i}"} for i in SIBLINGS + [TARGET]],
        }
    ]
    badges = _static_badges(groups, [{"provider": "magpie", "model": TARGET}], custom_providers=[MAGPIE])
    rows = [{"value": m["id"], "providerId": "custom:magpie"} for m in groups[0]["models"]]
    picker = _picker_rows(badges, rows)
    assert picker["badged"] == [[f"@custom:magpie:{TARGET}", "Fallback 1"]]
    assert picker["synthesized"] == []


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_combined_tree_bare_primary_model_shows_one_badged_row_and_no_extra_row():
    groups = _relay_groups(["anthropic/claude-sonnet-4", "openai/gpt-4o"])
    badges = _static_badges(groups, [], custom_providers=[RELAY], active_provider="custom:relay", default_model="claude-sonnet-4")
    rows = [{"value": m["id"], "providerId": "custom:relay"} for m in groups[0]["models"]]
    picker = _picker_rows(badges, rows)
    assert picker["badged"] == [["@custom:relay:anthropic/claude-sonnet-4", "Primary"]]
    assert picker["synthesized"] == []


# ---------------------------------------------------------------------------
# Re-review of #8741: the production picker, not only its helpers. The real
# renderModelDropdown() from static/ui.js runs under the fake DOM that
# tests/test_configured_model_picker_dedup.py uses, fed this backend's groups
# and badge map (static builder and live get_available_models()). Each test
# asserts how many "Configured" choices the dropdown lays out and what clicking
# each of them hands to selectModelFromDropdown: (model id, provider id).
# ---------------------------------------------------------------------------

_RENDER_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('(', start) + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < ui.length) {
    if (ui[i] === '(') parenDepth++;
    else if (ui[i] === ')') parenDepth--;
    i++;
  }
  let depth = 1;
  i = ui.indexOf('{', i);
  if (i < 0) throw new Error(name + ' body not found');
  i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  if (ui[i] === ';') i++;
  return ui.slice(start, i);
}

function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    _set: set,
    add(cls) { set.add(cls); },
    remove(cls) { set.delete(cls); },
    contains(cls) { return set.has(cls); },
    toggle(cls, force) {
      if (force === true) { set.add(cls); return true; }
      if (force === false) { set.delete(cls); return false; }
      if (set.has(cls)) { set.delete(cls); return false; }
      set.add(cls);
      return true;
    },
  };
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    parentElement: null,
    textContent: '',
    value: '',
    tabIndex: 0,
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    hasAttribute(name) { return this[name] !== undefined && this[name] !== null; },
    focus() { this._focused = true; },
  };
  node.classList = makeClassList();
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const input = makeNode('input');
        input.className = 'model-search-input';
        const clear = makeNode('button');
        clear.className = 'model-search-clear';
        this._qs['.model-search-input'] = input;
        this._qs['.model-search-clear'] = clear;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const input = makeNode('input');
        input.className = 'model-custom-input';
        const btn = makeNode('button');
        btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = input;
        this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeSelect(groups) {
  const sel = { id: 'modelSelect', children: [], options: [], _value: '' };
  Object.defineProperty(sel, 'value', { get() { return sel._value; }, set(v) { sel._value = String(v || ''); } });
  Object.defineProperty(sel, 'selectedOptions', { get() { const o = sel.options.find(x => x.value === sel._value); return o ? [o] : []; } });
  sel.appendChild = function (option) { option.parentElement = sel; sel.children.push(option); sel.options.push(option); };
  sel.querySelectorAll = function () { return []; };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models && group.extra_models.length) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      const opt = makeNode('option');
      opt.value = model.id;
      opt.textContent = model.label || model.id;
      og.appendChild(opt);
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = { createElement(tag) { return makeNode(tag); } };
function esc(v) { return String(v || ''); }
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
function _deduplicateModelPickerOptions() { return 0; }

for (const name of [
  '_normalizeConfiguredModelKey',
  '_configuredModelIdentity',
  '_getConfiguredModelBadge',
  '_providerFromModelValue',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  '_getOptionProviderId',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// Capture what a click hands to the selection path, the way the composer
// picker wires it (opts.selectModel defaults to selectModelFromDropdown).
const saved = [];
renderModelDropdown({ selectModel: (value, provider) => { saved.push([value, provider]); } });

// The "Configured" section is the run of rows between its heading and the next
// group heading, in document order, exactly as the dropdown lays them out.
const configuredHeading = t('model_group_configured');
const configured = [];
let inConfigured = false;
for (const n of dropdown.children) {
  if (n.classList.contains('model-group')) {
    inConfigured = n.textContent === configuredHeading;
    continue;
  }
  if (!inConfigured || !n.classList.contains('model-opt')) continue;
  const html = String(n._innerHTML || '');
  const before = saved.length;
  n.onclick();
  configured.push({
    id: (html.match(/class="model-opt-id">([^<]*)</) || [])[1] || '',
    saves: saved.slice(before),
  });
}
const allRows = [];
const stack = [...dropdown.children];
while (stack.length) {
  const n = stack.shift();
  const html = String(n._innerHTML || '');
  if (typeof n.onclick === 'function' && html.includes('model-opt-id')) {
    allRows.push((html.match(/class="model-opt-id">([^<]*)</) || [])[1] || '');
  }
  if (n.children && n.children.length) stack.push(...n.children);
}
process.stdout.write(JSON.stringify({ configured, allRows }));
"""


def _render_picker(tmp_path, groups: list[dict], badges: dict) -> dict:
    """Production ``renderModelDropdown()`` on *groups* + *badges*.

    Returns ``configured``: the "Configured" choices in dropdown order, each
    with the row id and the ``[model, provider]`` pairs its click saved, and
    ``allRows``: every clickable row in the dropdown.
    """
    driver = tmp_path / "picker_driver.js"
    driver.write_text(_RENDER_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(Path(__file__).resolve().parents[1] / "static" / "ui.js"), json.dumps({"groups": groups, "configuredBadges": badges})],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _relay_choices(picker: dict) -> list[tuple[str, list]]:
    """The "Configured" choices owned by the relay, as ``(row id, clicks)``."""
    return [(c["id"], c["saves"]) for c in picker["configured"] if any(p == "custom:relay" for _m, p in c["saves"])]


REVERSE_SERVED = ["claude-sonnet-4", "openai/gpt-4o"]
FORWARD_SERVED = ["anthropic/claude-sonnet-4", "openai/gpt-4o"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_picker_reverse_spelling_keeps_one_configured_choice_static(tmp_path):
    # Re-review shape: configured ``anthropic/claude-sonnet-4``, relay serves
    # ``claude-sonnet-4``. With the bare row badged (the previous head) the
    # dropdown laid out two "Configured" choices that saved
    # ``@custom:relay:claude-sonnet-4`` and ``anthropic/claude-sonnet-4``.
    groups = _relay_groups(REVERSE_SERVED)
    badges = _static_badges(groups, REVERSE_FALLBACK, custom_providers=[RELAY])
    picker = _render_picker(tmp_path, groups, badges)
    assert _relay_choices(picker) == [("anthropic/claude-sonnet-4", [["anthropic/claude-sonnet-4", "custom:relay"]])]
    assert len(picker["configured"]) == 1
    # The bare row stays a selectable catalog row, just not a configured one.
    assert "@custom:relay:claude-sonnet-4" in picker["allRows"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_picker_reverse_spelling_keeps_one_configured_choice_live(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=REVERSE_FALLBACK,
        served={"relay.example": REVERSE_SERVED},
    )
    picker = _render_picker(tmp_path, result["groups"], result["configured_model_badges"])
    assert _relay_choices(picker) == [("anthropic/claude-sonnet-4", [["anthropic/claude-sonnet-4", "custom:relay"]])]
    # The primary entry (openai-codex, not mocked) is the only other choice.
    assert [c["id"] for c in picker["configured"] if c["id"] != "anthropic/claude-sonnet-4"] == ["gpt-5.4"]
    bare_row = next(i for i in _option_ids(_group(result, "custom:relay")) if i.endswith(":claude-sonnet-4"))
    assert bare_row in picker["allRows"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_picker_bare_configured_id_keeps_one_badged_choice_static(tmp_path):
    # The repaired direction (review point 2) through the production renderer:
    # the vendor row is the one "Configured" choice and saves its own id.
    groups = _relay_groups(FORWARD_SERVED)
    badges = _static_badges(groups, BARE_FALLBACK, custom_providers=[RELAY])
    picker = _render_picker(tmp_path, groups, badges)
    assert _relay_choices(picker) == [
        ("@custom:relay:anthropic/claude-sonnet-4", [["@custom:relay:anthropic/claude-sonnet-4", "custom:relay"]])
    ]
    assert len(picker["configured"]) == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_picker_bare_configured_id_keeps_one_badged_choice_live(monkeypatch, tmp_path):
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=BARE_FALLBACK,
        served={"relay.example": FORWARD_SERVED},
    )
    picker = _render_picker(tmp_path, result["groups"], result["configured_model_badges"])
    vendor_row = next(i for i in _option_ids(_group(result, "custom:relay")) if i.endswith("anthropic/claude-sonnet-4"))
    assert _relay_choices(picker) == [(vendor_row, [[vendor_row, "custom:relay"]])]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_picker_sibling_shape_has_one_configured_choice(tmp_path):
    # The issue's shape: the configured ``codex/`` id is the one choice, and
    # neither sibling is badged or synthesized.
    groups = [
        {
            "provider": "magpie",
            "provider_id": "custom:magpie",
            "models": [{"id": f"@custom:magpie:{i}"} for i in SIBLINGS + [TARGET]],
        }
    ]
    badges = _static_badges(groups, [{"provider": "magpie", "model": TARGET}], custom_providers=[MAGPIE])
    picker = _render_picker(tmp_path, groups, badges)
    assert [(c["id"], c["saves"]) for c in picker["configured"]] == [
        (f"@custom:magpie:{TARGET}", [[f"@custom:magpie:{TARGET}", "custom:magpie"]])
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("fallback", [DEEPER_FALLBACK, BARE_DEEPER_FALLBACK], ids=["vendor-path-target", "bare-target"])
def test_production_picker_deeper_vendor_row_keeps_one_configured_choice_static(tmp_path, fallback):
    # Re-gate shape: with the deeper row badged (the re-gated head) the dropdown
    # laid out two "Configured" choices that saved
    # ``@custom:relay:vendor_a/deepseek/deepseek-v4-pro`` and the configured id.
    configured = fallback[0]["model"]
    groups = _relay_groups(DEEPER_SERVED)
    badges = _static_badges(groups, fallback, custom_providers=[RELAY])
    picker = _render_picker(tmp_path, groups, badges)
    assert _relay_choices(picker) == [(configured, [[configured, "custom:relay"]])]
    assert len(picker["configured"]) == 1
    # The deeper row stays a selectable catalog row, just not a configured one.
    assert "@custom:relay:vendor_a/deepseek/deepseek-v4-pro" in picker["allRows"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("fallback", [DEEPER_FALLBACK, BARE_DEEPER_FALLBACK], ids=["vendor-path-target", "bare-target"])
def test_production_picker_deeper_vendor_row_keeps_one_configured_choice_live(monkeypatch, tmp_path, fallback):
    configured = fallback[0]["model"]
    result = _live_models(
        monkeypatch,
        tmp_path,
        custom_providers=[RELAY],
        fallback_providers=fallback,
        served={"relay.example": DEEPER_SERVED},
    )
    picker = _render_picker(tmp_path, result["groups"], result["configured_model_badges"])
    assert _relay_choices(picker) == [(configured, [[configured, "custom:relay"]])]
    # The primary entry (openai-codex, not mocked) is the only other choice.
    assert [c["id"] for c in picker["configured"] if c["id"] != configured] == ["gpt-5.4"]
    deeper_row = next(i for i in _option_ids(_group(result, "custom:relay")) if i.endswith("vendor_a/deepseek/deepseek-v4-pro"))
    assert deeper_row in picker["allRows"]
