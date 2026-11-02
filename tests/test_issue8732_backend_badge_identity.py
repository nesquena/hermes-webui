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
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import api.config as config

TARGET = "codex/gpt-6.1-sol"
SIBLINGS = ["any/gpt-6.1-sol", "auto/gpt-6.1-sol"]
# 34 ids: the siblings first (inside the visible cut), the configured id last
# (past the cut, so it lives in ``extra_models``).
MAGPIE_IDS = SIBLINGS + [f"vendor{i:02d}/model-{i:02d}" for i in range(31)] + [TARGET]


def _available_models(monkeypatch, tmp_path, fallback_provider: str, model_ids: list[str]) -> dict:
    """Live ``get_available_models()`` with ``magpie`` served from a local mock."""

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
        if "magpie.example" in url:
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
            "model": {"provider": "openai-codex", "default": "gpt-5.4"},
            "custom_providers": [
                {"name": "magpie", "base_url": "https://magpie.example/v1", "api_key": "magpie-key"}
            ],
            "fallback_providers": [{"provider": fallback_provider, "model": TARGET}],
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


def _magpie_group(result: dict) -> dict:
    groups = [g for g in result.get("groups", []) if g.get("provider_id") == "custom:magpie"]
    assert len(groups) == 1, [g.get("provider_id") for g in result.get("groups", [])]
    return groups[0]


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


def _static_badges(groups: list[dict], fallback_providers: list[dict]) -> dict:
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({"fallback_providers": fallback_providers})
    try:
        return config._configured_model_badges_from_static_catalog(
            groups, active_provider=None, default_model=""
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
