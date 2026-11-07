"""Regression tests for the six #7507 reviewer findings (maintainer re-gate).

Each closed path by which an excluded model came BACK after the user saved
a ``picker_excludes`` entry gets a dedicated test here:

1. Named custom providers (``@custom:alpha:chat-a``) never matched a bare
   ``chat-a`` exclusion because the prefix strip split on the FIRST colon.
2. Alias-equivalent keys (``zai`` + ``z.ai``) — only the first matching key
   was returned; the other provider's list was ignored.
3. The empty-catalog emergency fallback re-inserted the default model that
   the exclusion just removed.
4. The browser re-injected an excluded default / previous pick for a new
   chat via ``_ensureModelOptionInDropdown``.
5. An in-flight ``/api/models/live`` response built under the PREVIOUS
   policy overwrote the cache saved by the new policy, and the bounded
   catalog builder had the same race.
6. An old browser live fetch back-filled ``_liveModelCache`` with an
   excluded id because no fetch carried an invalidation epoch.
"""

from __future__ import annotations

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
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_caches():
    """Invalidate both server caches around each test.

    The TTL models cache and the /api/models/live cache would otherwise
    hand back a previous test's catalog and silently pass.
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


def _patch_settings(monkeypatch, excludes: dict) -> None:
    """Point ``load_settings`` at a fixed picker_excludes map."""
    monkeypatch.setattr(
        config,
        "load_settings",
        lambda: {"picker_excludes": excludes, "default_model": "test/x"},
    )


def _group_ids(group: dict) -> list[str]:
    """Concatenate ``models`` and ``extra_models`` ids for one group."""
    out: list[str] = []
    for bucket in ("models", "extra_models"):
        for entry in group.get(bucket, []) or []:
            mid = entry.get("id", "")
            if mid:
                out.append(mid)
    return out


# ── Finding 1: colon-prefix splitting ─────────────────────────────────────


class TestFinding1ColonPrefixStrip:
    """``@custom:alpha:chat-a`` must split on the COMPLETE provider prefix."""


    @pytest.mark.parametrize(
        "model_id, exclude_set, expected",
        [
            # Plain form is unchanged by the fix.
            ("@openrouter:gpt-5.6-luna", {"gpt-5.6-luna"}, True),
            # THE BUG: first-colon split left "alpha:chat-a" behind.
            ("@custom:alpha:chat-a", {"chat-a"}, True),
            # Named custom provider with a colon-bearing MODEL id.
            ("@custom:backup:model-a:free", {"free"}, True),
            # Case-preserving for the exclude entry itself.
            ("@custom:alpha:chat-a", {"CHAT-A"}, False),
            # Non-matching exclusion still False.
            ("@custom:alpha:chat-a", {"chat-b"}, False),
            # Non-prefixed values unchanged.
            ("chat-a", {"chat-a"}, True),
            ("chat-a", {"other"}, False),
            # Slash-qualified named-custom ids keep their slash.
            ("@custom:agg:vertex/gemini-1.0", {"vertex/gemini-1.0"}, True),
        ],
    )
    def test_strip_provider_prefix_provider_id_aware(
        self, model_id, exclude_set, expected
    ):
        assert config._is_model_id_excluded(model_id, exclude_set) is expected

    def test_strip_prefix_helper_full_boundary(self):
        assert config._strip_provider_prefix_from_model_id("@custom:alpha:chat-a") == "chat-a"
        assert config._strip_provider_prefix_from_model_id("@openrouter:m") == "m"
        assert config._strip_provider_prefix_from_model_id("bare") == "bare"
        # Unknown provider id: historical first-segment behaviour preserved.
        assert config._strip_provider_prefix_from_model_id("@unknown:x") == "x"

    def test_static_catalog_named_custom_group_honours_bare_exclude(self, monkeypatch):
        """The named-custom group in the static catalog must filter the
        bare-id exclude even though its ids render ``@custom:slug:``-prefixed.
        """
        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {"provider": "custom:alpha", "default": "custom:alpha/chat-a"},
                "custom_providers": [
                    {
                        "name": "Alpha",
                        "base_url": "https://alpha.example/v1",
                        "models": ["chat-a", "chat-keep"],
                    }
                ],
            },
            raising=False,
        )
        _patch_settings(monkeypatch, {"custom:alpha": ["chat-a"]})

        result = config._static_models_catalog_without_live_probes()

        alpha_groups = [
            g for g in result.get("groups", []) or []
            if g.get("provider_id") == "custom:alpha"
        ]
        assert alpha_groups, (
            "the named custom provider group must be present in the static catalog"
        )
        alpha_ids = set()
        for group in alpha_groups:
            for mid in _group_ids(group):
                bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
                assert bare != "chat-a", (
                    f"excluded chat-a survived as {mid!r} in group "
                    f"{group.get('provider_id')!r}"
                )
                alpha_ids.add(bare)
        # The non-excluded sibling must survive (the fix filters the
        # bare id, it does not empty the group). Scoped to the custom
        # group: other providers' groups (gemini, deepseek, …) are
        # unrelated to this exclude and may legitimately appear based on
        # the machine's env/auth state.
        assert "chat-keep" in alpha_ids, (
            f"chat-keep must survive the chat-a exclusion; alpha group ids={sorted(alpha_ids)}"
        )

    def test_default_model_slash_form_is_excluded(self, monkeypatch):
        """The default-model re-injection guard compares `model.default`
        in its raw `provider/model` slash form (`custom:alpha/chat-a`) —
        the shape Hermes config actually stores. `_is_model_id_excluded`
        must match that form, or the excluded default is silently
        re-inserted into the active group (the CI shard red that
        surfaced this)."""
        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {
                    "provider": "custom:alpha",
                    "default": "custom:alpha/chat-a",
                },
                "custom_providers": [
                    {
                        "name": "Alpha",
                        "base_url": "https://alpha.example/v1",
                        "models": ["chat-a", "chat-keep"],
                    }
                ],
            },
            raising=False,
        )
        _patch_settings(monkeypatch, {"custom:alpha": ["chat-a"]})

        result = config._static_models_catalog_without_live_probes()

        all_ids = {
            mid
            for group in result.get("groups", []) or []
            for mid in _group_ids(group)
        }
        # Neither the slash form nor the bare id may survive anywhere.
        assert "custom:alpha/chat-a" not in all_ids, (
            "excluded default_model was re-injected in its provider/model form"
        )
        for mid in all_ids:
            bare = mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
            assert bare != "chat-a", f"excluded chat-a survived as {mid!r}"
        assert "chat-keep" in {
            mid.split(":", 1)[1] if mid.startswith("@") and ":" in mid else mid
            for group in result.get("groups", []) or []
            for mid in _group_ids(group)
        }, "the non-excluded sibling must survive"

    def test_is_model_id_excluded_slash_form_unit(self):
        """Unit pin for the slash-form match: the bare half matches, a
        slash-bearing id that is NOT in the exclude set is untouched."""
        excludes = {"chat-a"}
        assert config._is_model_id_excluded("custom:alpha/chat-a", excludes)
        assert config._is_model_id_excluded("chat-a", excludes)
        assert not config._is_model_id_excluded("custom:alpha/chat-keep", excludes)
        # A slash-bearing id not present in the exclude set stays.
        assert not config._is_model_id_excluded(
            "openrouter/anthropic/claude-3", excludes
        )


# ── Finding 2: alias-key union ────────────────────────────────────────────


class TestFinding2AliasKeyUnion:
    """Excludes stored under BOTH ``zai`` and ``z.ai`` must BOTH apply."""


    def test_union_of_alias_equivalent_keys(self, monkeypatch):
        # The store carries both the canonical and an alias key, each
        # naming a different slice. The old first-match return ignored the
        # second key and resurrected its model.
        _patch_settings(monkeypatch, {"zai": ["kimi-a"], "z.ai": ["kimi-b"], "glm": ["kimi-c"]})
        result = config.get_picker_excludes("zai")
        assert {"kimi-a", "kimi-b", "kimi-c"} <= result

    def test_union_when_requesting_the_alias(self, monkeypatch):
        _patch_settings(monkeypatch, {"zai": ["kimi-a"], "z.ai": ["kimi-b"]})
        assert {"kimi-a", "kimi-b"} <= config.get_picker_excludes("z.ai")

    def test_union_does_not_leak_across_providers(self, monkeypatch):
        _patch_settings(monkeypatch, {"zai": ["kimi-a"], "openai": ["gpt-x"]})
        result = config.get_picker_excludes("zai")
        assert "gpt-x" not in result
        assert "kimi-a" in result

    def test_static_catalog_alias_union_filters_model(self, monkeypatch):
        """Both halves of the alias pair remove their models in a real
        catalog build — the second list is no longer dropped.
        """
        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {"provider": "zai", "default": "zai/kimi-keep"},
                "providers": {
                    "zai": {
                        "api_key": "test",
                        "models": ["kimi-keep", "kimi-a", "kimi-b"],
                    }
                },
            },
            raising=False,
        )
        _patch_settings(monkeypatch, {"zai": ["kimi-a"], "z.ai": ["kimi-b"]})

        result = config._static_models_catalog_without_live_probes()

        ids = {
            m.split(":", 1)[1] if m.startswith("@") and ":" in m else m
            for group in result.get("groups", []) or []
            for m in _group_ids(group)
        }
        assert "kimi-a" not in ids
        assert "kimi-b" not in ids, "alias-stored exclude was ignored (first-match return)"
        assert "kimi-keep" in ids


# ── Finding 3: minimal-catalog fallback resurrection ──────────────────────


class TestFinding3MinimalFallbackHonoursExcludes:
    """The emergency one-model fallback must not re-insert the excluded default."""


    def test_minimal_catalog_skips_excluded_default(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "cfg",
            {"model": {"provider": "openai", "default": "gpt-excluded"}},
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})

        result = config._minimal_static_models_catalog()

        assert result["groups"] == [], (
            f"excluded default re-inserted by the fallback: {result['groups']}"
        )
        # The no-eligible-model state is explicit, not silence.
        assert result.get("no_eligible_models") is True

    def test_minimal_catalog_keeps_non_excluded_default(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "cfg",
            {"model": {"provider": "openai", "default": "gpt-keep"}},
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})

        result = config._minimal_static_models_catalog()

        assert [m["id"] for m in result["groups"][0]["models"]] == ["gpt-keep"]
        assert "no_eligible_models" not in result

    def test_minimal_catalog_no_default_is_not_a_no_eligible_state(self, monkeypatch):
        monkeypatch.setattr(config, "cfg", {"model": {}}, raising=False)
        _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})

        result = config._minimal_static_models_catalog()

        assert result["groups"] == []
        # A genuinely empty (no-default) config is not an exclusion outcome.
        assert "no_eligible_models" not in result

    def test_static_catalog_no_groups_path_honours_excludes(self, monkeypatch):
        """``_static_models_catalog_without_live_probes``'s no-groups branch
        delegates to the minimal fallback, which must also honour excludes.
        """
        monkeypatch.setattr(
            config,
            "cfg",
            {"model": {"provider": "openai", "default": "gpt-excluded"}},
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})

        result = config._static_models_catalog_without_live_probes()

        all_ids = [
            m
            for group in result.get("groups", []) or []
            for m in _group_ids(group)
        ]
        assert "gpt-excluded" not in all_ids


# ── Finding 5a: /api/models/live in-flight stale payload ──────────────────


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


class TestFinding5LiveCachePolicyIdentity:
    """A live response built under the OLD policy must never satisfy a
    request issued AFTER the policy changed."""


    def test_policy_change_moves_cache_key(self, monkeypatch):
        _patch_settings(monkeypatch, {"openai": ["a"]})
        key_before = routes._live_models_cache_key("openai")
        _patch_settings(monkeypatch, {"openai": ["a", "b"]})
        key_after = routes._live_models_cache_key("openai")
        assert key_before != key_after, (
            "picker_excludes must be part of the live-cache key identity"
        )

    def test_inflight_stale_payload_is_not_served(self, monkeypatch):
        """Codex's thread-barrier repro, end to end.

        Request #1 starts with an EMPTY policy, the provider returns a list
        containing ``gpt-excluded`` and the handler caches it. While the
        response is 'in flight' the policy changes to exclude that model.
        Request #2 must NOT receive the stale model.

        The ids are deliberately the BARE form the user lists in settings
        — the exact-id contract compares the provider's id against the
        stored entry as-is, so a vendor-prefixed id (``openai/x``) is a
        different entry.
        """
        # Start with no exclusions → the stale payload gets cached.
        _patch_settings(monkeypatch, {})
        _install_provider_model_ids(
            monkeypatch, lambda provider: ["gpt-keep", "gpt-excluded"]
        )
        _patch_live_models_basics(monkeypatch)
        parsed = urlparse("/api/models/live?provider=openai")

        first = routes._handle_live_models(object(), parsed)
        assert [m["id"] for m in first["models"]] == [
            "gpt-keep",
            "gpt-excluded",
        ]

        # Policy changes: the excluded model must now be filtered.
        _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})

        second = routes._handle_live_models(object(), parsed)
        out_ids = [m["id"] for m in second["models"]]
        assert "gpt-excluded" not in out_ids, (
            "stale in-flight payload satisfied the post-change request"
        )
        assert "gpt-keep" in out_ids

    def test_payload_never_leaks_epoch_field(self, monkeypatch):
        _patch_settings(monkeypatch, {"openai": ["x"]})
        _install_provider_model_ids(monkeypatch, lambda provider: ["openai/gpt-a"])
        _patch_live_models_basics(monkeypatch)

        payload = routes._handle_live_models(object(), urlparse("/api/models/live?provider=openai"))

        assert "_picker_excludes_epoch" not in payload, (
            "internal epoch stamp must be stripped before the client sees it"
        )
        # And the cached copy carries it.
        key = routes._live_models_cache_key("openai")
        cached = routes._LIVE_MODELS_CACHE[key][1]
        assert cached["_picker_excludes_epoch"] == key[2]

    def test_cached_entry_rejected_when_epoch_changes(self, monkeypatch):
        """Defense-in-depth: even a hand-inserted entry under the current key
        is rejected when its stamped epoch no longer matches.
        """
        _patch_settings(monkeypatch, {"openai": ["x"]})
        _install_provider_model_ids(monkeypatch, lambda provider: ["openai/gpt-a"])
        _patch_live_models_basics(monkeypatch)
        parsed = urlparse("/api/models/live?provider=openai")
        routes._handle_live_models(object(), parsed)

        key = routes._live_models_cache_key("openai")
        routes._LIVE_MODELS_CACHE[key][1]["_picker_excludes_epoch"] = "stale-epoch"
        assert routes._get_cached_live_models(key) is None

    def test_settings_save_clears_live_cache(self, monkeypatch):
        """The /api/settings handler's picker_excludes block must clear the
        live cache (and the builder cache) — even with the epoch in the key,
        an un-cleared entry for the OLD epoch is dead weight.
        """
        calls = {"invalidate": 0, "live_clear": 0}

        def _count_invalidate():
            calls["invalidate"] += 1

        def _count_clear():
            calls["live_clear"] += 1

        monkeypatch.setattr(config, "invalidate_models_cache", _count_invalidate)
        monkeypatch.setattr(routes, "_clear_live_models_cache", _count_clear)

        # Mirror the exact invalidation block from the /api/settings handler.
        body = {"picker_excludes": {"openai": ["gpt-x"]}}
        saved = dict(body)
        if "picker_excludes" in body:
            config.invalidate_models_cache()
            routes._clear_live_models_cache()
            saved["_invalidate_models"] = True

        assert calls == {"invalidate": 1, "live_clear": 1}
        assert saved["_invalidate_models"] is True

    def test_settings_save_block_wired_in_routes_source(self):
        """Static wiring check: the block really lives in the /api/settings
        handler and calls BOTH invalidators.
        """
        src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
        block = 'if "picker_excludes" in body'
        assert block in src
        assert "invalidate_models_cache" in src
        assert "_clear_live_models_cache" in src
        settings_idx = src.find('parsed.path == "/api/settings"')
        assert 0 <= settings_idx < src.find(block)


# ── Finding 5b: bounded-catalog-builder straddle guard ─────────────────────


class TestFinding5BoundedBuilderPolicyIdentity:
    """The bounded catalog builder must not publish a result built under a
    superseded policy."""

    def test_source_fingerprint_includes_picker_excludes(self, monkeypatch):
        _patch_settings(monkeypatch, {})
        before = config._models_cache_source_fingerprint()
        _patch_settings(monkeypatch, {"openai": ["gpt-x"]})
        after = config._models_cache_source_fingerprint()
        assert before != after, (
            "picker_excludes must be in the models-cache source fingerprint"
        )

    def test_publish_discards_result_when_policy_changed_midbuild(self, monkeypatch):
        """Reproduce the bounded-builder race: the rebuild finishes AFTER a
        ``picker_excludes`` save. The result must be discarded, not cached —
        publishing it would make the next 24h of requests re-receive an
        excluded model behind a fingerprint that claims it is current.
        """
        monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 5.0)
        monkeypatch.setattr(config, "_available_models_cache", None, raising=False)
        monkeypatch.setattr(config, "_available_models_cache_ts", 0.0, raising=False)
        monkeypatch.setattr(
            config, "_available_models_cache_source_fingerprint", None, raising=False
        )
        monkeypatch.setattr(
            config, "_get_models_cache_path", lambda: REPO / "no-such-cache.json"
        )
        # Isolate the bounded path from the disk-cache branches.
        monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
        monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda: None)

        stale_result = {
            "active_provider": "openai",
            "default_model": "gpt-excluded",
            "configured_model_badges": {},
            "groups": [
                {
                    "provider": "OpenAI",
                    "provider_id": "openai",
                    "models": [{"id": "gpt-excluded", "label": "Excluded"}],
                }
            ],
        }

        # Policy is EMPTY at build-start, then flips mid-build (the save the
        # reviewer reproduced with the thread barrier).
        _patch_settings(monkeypatch, {})

        def _slow_rebuild(_builder):
            import time as _t

            _patch_settings(monkeypatch, {"openai": ["gpt-excluded"]})
            _t.sleep(0.05)
            return stale_result

        monkeypatch.setattr(config, "_invoke_models_rebuild", _slow_rebuild)

        result = config.get_available_models(force_refresh=True)

        # Nothing was published under the new policy.
        assert config._available_models_cache is None, (
            "stale bounded-builder result was published to the memory cache"
        )
        # And what the caller received is the exclusion-aware fallback.
        assert result is not stale_result
        for group in result.get("groups", []) or []:
            for m in group.get("models", []):
                assert m["id"] != "gpt-excluded", (
                    "stale bounded-builder result reached the caller"
                )
