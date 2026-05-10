"""Regression tests for #1567 — Nous Portal picker capacity + endpoint symmetry.

Two issues addressed in one PR:

1. **Endpoint disagreement (the bug):** The Settings → Providers card and the
   model picker dropdown returned different Nous catalogs because their
   detection paths differ. ``api/providers.py:get_providers`` iterates ALL
   OAuth providers regardless of `list_available_providers().authenticated`.
   ``api/config.py:_build_available_models_uncached`` only includes providers
   in ``detected_providers``, which is gated on
   ``list_available_providers().authenticated``. On some hermes_cli versions
   that flag disagrees with ``get_auth_status(<id>).logged_in``. Result: the
   providers card shows the live catalog (e.g. 396 models) and the picker
   shows nothing or the stale 4-entry static fallback.

2. **UX cap (the design concern):** Even with the disagreement fixed, dumping
   a 397-model dropdown into the picker would be unusable. We cap the
   dropdown at ~15 featured entries (deterministic vendor-priority sample,
   sticky for the user's currently-selected model) and return the full
   catalog under ``extra_models`` so /model autocomplete and the dynamic
   label map still cover everything.

Tests in this file pin both invariants.
"""

from __future__ import annotations

import sys
import types

import api.config as config
import api.profiles as profiles


# Big catalog matches the shape of an enterprise Nous Portal account.
# Volume distribution mirrors what we saw on Nathan's machine (~30 models)
# extrapolated up to ~400 with the same vendor mix Deor reported.
_BIG_CATALOG_VENDORS = {
    "anthropic": 8,
    "openai": 30,
    "google": 12,
    "moonshotai": 5,
    "z-ai": 15,
    "minimax": 10,
    "qwen": 80,
    "x-ai": 8,
    "deepseek": 20,
    "stepfun": 10,
    "xiaomi": 6,
    "tencent": 12,
    "nvidia": 25,
    "arcee-ai": 8,
    "meta-llama": 50,
    "mistralai": 40,
    "cohere": 25,
    "databricks": 15,
    "lambda-ai": 18,
}


def _build_big_catalog() -> list[str]:
    out = []
    for v, n in _BIG_CATALOG_VENDORS.items():
        for i in range(n):
            out.append(f"{v}/model-{v}-{i:02d}")
    return out


def _install_fake_hermes_cli(
    monkeypatch,
    *,
    nous_ids: list[str] | None = None,
    raise_on_lookup: bool = False,
    list_authenticated: bool = True,
    auth_status_logged_in: bool = True,
):
    """Install fake ``hermes_cli`` modules with controllable Nous behavior.

    The two flags ``list_authenticated`` and ``auth_status_logged_in`` model
    the divergence between ``hermes_cli.models.list_available_providers()``
    and ``hermes_cli.auth.get_auth_status()`` that #1567 calls out as a
    real-world pattern on some hermes_cli versions.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {
            "id": "nous",
            "label": "Nous Portal",
            "aliases": [],
            "authenticated": list_authenticated,
        },
    ]
    if raise_on_lookup:

        def _raise(_pid):
            raise RuntimeError("simulated hermes_cli failure")

        fake_models.provider_model_ids = _raise
    else:
        ids = list(nous_ids) if nous_ids is not None else []
        fake_models.provider_model_ids = lambda pid: ids if pid == "nous" else []

    fake_auth = types.ModuleType("hermes_cli.auth")

    def _get_auth_status(pid):
        if pid == "nous":
            return {"logged_in": auth_status_logged_in, "key_source": "oauth"}
        return {}

    fake_auth.get_auth_status = _get_auth_status

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


def _swap_in_test_config(extra_cfg):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg.update(extra_cfg)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def _restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    return _restore


def _scrub_provider_env(monkeypatch):
    """Drop every provider env var so detection doesn't leak unrelated keys."""
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        "OLLAMA_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_GO_API_KEY",
        "NOUS_API_KEY",
        "NVIDIA_API_KEY",
        "LM_API_KEY",
        "LMSTUDIO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ────────────────────────────────────────────────────────────────────────
# Section 1 — _build_nous_featured_set helper invariants
# ────────────────────────────────────────────────────────────────────────


class TestBuildNousFeaturedSet:
    """Unit tests for the deterministic featured-vs-extras split helper."""

    def test_small_catalog_is_no_op(self):
        from api.config import _build_nous_featured_set, _NOUS_FEATURED_THRESHOLD

        # 20 entries — below the threshold, helper should return the input
        # untouched and an empty extras list.
        catalog = [f"vendor/model-{i:02d}" for i in range(20)]
        assert len(catalog) <= _NOUS_FEATURED_THRESHOLD
        featured, extras = _build_nous_featured_set(catalog)
        assert featured == catalog
        assert extras == []

    def test_large_catalog_is_capped_to_target(self):
        from api.config import _build_nous_featured_set, _NOUS_FEATURED_TARGET

        catalog = _build_big_catalog()
        assert len(catalog) > 100, "test fixture should produce a large catalog"
        featured, extras = _build_nous_featured_set(catalog)
        assert len(featured) == _NOUS_FEATURED_TARGET, (
            f"Large catalog should produce exactly _NOUS_FEATURED_TARGET "
            f"featured entries, got {len(featured)}."
        )
        assert len(extras) == len(catalog) - _NOUS_FEATURED_TARGET

    def test_featured_and_extras_are_disjoint_and_complete(self):
        from api.config import _build_nous_featured_set

        catalog = _build_big_catalog()
        featured, extras = _build_nous_featured_set(catalog)
        assert set(featured) & set(extras) == set(), (
            "featured and extras must be disjoint — every model belongs to "
            "exactly one bucket."
        )
        assert set(featured) | set(extras) == set(catalog), (
            "featured ∪ extras must equal the input catalog — no model "
            "should be silently dropped."
        )

    def test_priority_vendors_get_picked_first(self):
        from api.config import _build_nous_featured_set, _NOUS_VENDOR_PRIORITY

        catalog = _build_big_catalog()
        featured, _ = _build_nous_featured_set(catalog)
        # Every priority vendor with ≥1 entry in the catalog must appear in
        # featured (round-robin guarantee until we hit the slot budget).
        featured_vendors = {m.split("/", 1)[0] for m in featured}
        for v in _NOUS_VENDOR_PRIORITY:
            if v in _BIG_CATALOG_VENDORS:
                assert v in featured_vendors, (
                    f"Priority vendor {v!r} missing from featured set — "
                    f"round-robin guarantee violated."
                )

    def test_sticky_selection_is_preserved(self):
        from api.config import _build_nous_featured_set

        catalog = _build_big_catalog()
        # Pick a model from a leftover (non-priority) vendor that wouldn't
        # normally make the featured cut.
        sticky = "lambda-ai/model-lambda-ai-15"
        assert sticky in catalog
        featured, extras = _build_nous_featured_set(catalog, selected_model_id=sticky)
        assert sticky in featured, (
            f"Sticky-selected model {sticky!r} must appear in featured — "
            f"otherwise the user's choice gets orphaned out of the dropdown "
            f"after a refresh."
        )
        assert sticky not in extras

    def test_sticky_selection_handles_at_nous_prefix(self):
        from api.config import _build_nous_featured_set

        catalog = _build_big_catalog()
        # The frontend stores selections as @nous:vendor/model — helper must
        # strip the prefix to match against the bare-id catalog.
        sticky_with_prefix = "@nous:lambda-ai/model-lambda-ai-15"
        bare = "lambda-ai/model-lambda-ai-15"
        featured, _ = _build_nous_featured_set(
            catalog, selected_model_id=sticky_with_prefix
        )
        assert bare in featured

    def test_curated_static_flagships_are_preserved(self):
        from api.config import _build_nous_featured_set, _PROVIDER_MODELS

        # Build a catalog that contains all the curated static IDs so the
        # rule-2 path fires.
        static_ids = []
        for entry in _PROVIDER_MODELS.get("nous", []):
            sid = entry["id"]
            if sid.startswith("@nous:"):
                sid = sid[len("@nous:") :]
            static_ids.append(sid)
        catalog = static_ids + [f"filler-vendor/filler-{i:03d}" for i in range(100)]
        featured, _ = _build_nous_featured_set(catalog)
        for sid in static_ids:
            assert sid in featured, (
                f"Curated static flagship {sid!r} dropped from featured set."
            )

    def test_empty_catalog_returns_empty(self):
        from api.config import _build_nous_featured_set

        f, e = _build_nous_featured_set([])
        assert f == [] and e == []

    def test_deterministic_across_calls(self):
        from api.config import _build_nous_featured_set

        catalog = _build_big_catalog()
        f1, e1 = _build_nous_featured_set(catalog)
        f2, e2 = _build_nous_featured_set(catalog)
        assert f1 == f2 and e1 == e2, (
            "Featured set must be deterministic — random/seeded selection "
            "would cause cache thrash and dropdown flicker on every reload."
        )


# ────────────────────────────────────────────────────────────────────────
# Section 2 — End-to-end /api/models behaviour with the cap applied
# ────────────────────────────────────────────────────────────────────────


class TestApiModelsLargeCatalog:
    """Wired-up test exercising the dispatch branch at config.py:2243."""

    def test_picker_caps_large_catalog_and_exposes_extras(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        catalog = _build_big_catalog()
        _install_fake_hermes_cli(monkeypatch, nous_ids=catalog)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data["groups"] if g["provider_id"] == "nous"]
            assert len(nous_groups) == 1
            grp = nous_groups[0]
            from api.config import _NOUS_FEATURED_TARGET

            assert len(grp["models"]) == _NOUS_FEATURED_TARGET, (
                f"Picker should render {_NOUS_FEATURED_TARGET} featured entries "
                f"on a {len(catalog)}-model catalog, got {len(grp['models'])}."
            )
            assert "extra_models" in grp, (
                "Capped Nous group must include 'extra_models' so /model "
                "autocomplete and the label map cover the full catalog."
            )
            assert len(grp["extra_models"]) == len(catalog) - _NOUS_FEATURED_TARGET
            # Optgroup label is decorated with the truncation count so the user
            # knows the dropdown is intentionally trimmed.
            assert f"{_NOUS_FEATURED_TARGET} of {len(catalog)}" in grp["provider"], (
                f"Provider label should include '({_NOUS_FEATURED_TARGET} of "
                f"{len(catalog)})' for capped catalogs, got {grp['provider']!r}."
            )
        finally:
            restore()

    def test_picker_does_not_cap_small_catalog(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        # 20 models — below threshold, should pass through with no extras.
        small_catalog = [f"vendor-{i % 4}/model-{i:02d}" for i in range(20)]
        _install_fake_hermes_cli(monkeypatch, nous_ids=small_catalog)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            grp = next(g for g in data["groups"] if g["provider_id"] == "nous")
            assert len(grp["models"]) == 20
            assert "extra_models" not in grp or grp["extra_models"] == []
            assert "of " not in grp["provider"], (
                "Optgroup label should NOT include a truncation count when no "
                "trimming happened, got " + repr(grp["provider"])
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 3 — Auth-detection symmetry (#1567 part 1)
# ────────────────────────────────────────────────────────────────────────


class TestNousDetectionSymmetry:
    """The picker must include Nous whenever the providers card would —
    fixes the asymmetric-detection bug at the heart of #1567."""

    def test_picker_includes_nous_when_get_auth_status_logged_in(
        self, monkeypatch, tmp_path
    ):
        """list_available_providers() reports authenticated=False but
        get_auth_status('nous').logged_in=True. Picker must still show Nous."""
        _scrub_provider_env(monkeypatch)
        catalog = ["anthropic/claude-opus-4.7", "openai/gpt-5.5"]
        _install_fake_hermes_cli(
            monkeypatch,
            nous_ids=catalog,
            list_authenticated=False,  # primary detection path says NO
            auth_status_logged_in=True,  # secondary detection path says YES
        )
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data["groups"] if g["provider_id"] == "nous"]
            assert nous_groups, (
                "Picker must include Nous group when get_auth_status reports "
                "logged_in=True, even if list_available_providers disagrees. "
                "This is the asymmetric-detection bug from #1567."
            )
            assert len(nous_groups[0]["models"]) == 2
        finally:
            restore()

    def test_picker_omits_nous_when_both_auth_signals_false(
        self, monkeypatch, tmp_path
    ):
        """When neither signal reports authenticated, Nous should NOT appear.
        Previously the static 4-entry list could leak in via the fallback path
        even for unauthenticated users — that fallback is now scoped to the
        hermes_cli-unavailable case only."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(
            monkeypatch,
            nous_ids=[],  # no live catalog (also no auth)
            list_authenticated=False,
            auth_status_logged_in=False,
        )
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "anthropic"}})
        try:
            # Active provider is anthropic, not nous — so detected_providers
            # only includes nous if the new auth-symmetry check fires.
            data = config.get_available_models()
            nous_groups = [g for g in data["groups"] if g["provider_id"] == "nous"]
            assert not nous_groups, (
                "Nous must NOT appear in picker when neither auth signal "
                "reports authenticated. Got: " + str(nous_groups)
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 4 — Live-fetch-empty handling (#1567 part 2)
# ────────────────────────────────────────────────────────────────────────


class TestNousLiveFetchEmpty:
    """When authenticated but live-fetch returns [] (transient hermes_cli
    state, OAuth refresh in flight), DON'T fall back to the stale 4-entry
    static list — that creates the providers-card-vs-picker disagreement
    that #1567 reports. Omit the group entirely instead."""

    def test_authenticated_empty_catalog_omits_nous_group(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(
            monkeypatch,
            nous_ids=[],  # live-fetch returns empty list (no exception)
            auth_status_logged_in=True,  # but user IS authenticated
        )
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data["groups"] if g["provider_id"] == "nous"]
            assert not nous_groups, (
                "Authenticated user with empty live-fetch should NOT see "
                "the stale 4-entry static list — that's exactly the "
                "providers-card-vs-picker disagreement #1567 reports. "
                "Omit the Nous group entirely; it'll re-populate on the "
                "next cache rebuild when the live-fetch returns something."
            )
        finally:
            restore()

    def test_hermes_cli_unavailable_falls_back_to_static_4(self, monkeypatch, tmp_path):
        """When hermes_cli is unavailable (raises) — distinct from returning [] —
        we DO fall back to the static 4-entry list so the picker isn't empty
        in that degraded environment. This preserves pre-#1538 behavior for
        test envs without hermes_cli."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(
            monkeypatch,
            raise_on_lookup=True,
            auth_status_logged_in=True,
        )
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data["groups"] if g["provider_id"] == "nous"]
            assert nous_groups, (
                "When hermes_cli raises, Nous group MUST still appear with "
                "the curated static fallback so the picker isn't empty in "
                "test envs that lack the agent package."
            )
            assert len(nous_groups[0]["models"]) == 4, (
                "Static fallback should expose the curated 4-entry list "
                "from _PROVIDER_MODELS['nous']."
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 5 — Providers card ↔ picker symmetry
# ────────────────────────────────────────────────────────────────────────


class TestProvidersCardPickerSymmetry:
    """Both endpoints must report the same featured set + total count for
    Nous Portal. This is the load-bearing invariant that ends the visual
    disagreement #1567 reports."""

    def test_providers_card_and_picker_agree_on_featured_set(
        self, monkeypatch, tmp_path
    ):
        _scrub_provider_env(monkeypatch)
        catalog = _build_big_catalog()
        _install_fake_hermes_cli(monkeypatch, nous_ids=catalog)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            from api.providers import get_providers
            from api.config import _NOUS_FEATURED_TARGET

            providers = {p["id"]: p for p in get_providers()["providers"]}
            picker = config.get_available_models()
            picker_nous = next(
                g for g in picker["groups"] if g["provider_id"] == "nous"
            )

            card = providers["nous"]
            # Both render exactly _NOUS_FEATURED_TARGET visible models.
            assert len(card["models"]) == _NOUS_FEATURED_TARGET
            assert len(picker_nous["models"]) == _NOUS_FEATURED_TARGET

            # Both report the full catalog size somewhere.
            assert card["models_total"] == len(catalog), (
                f"Providers card models_total should match live catalog size, "
                f"got {card['models_total']} vs catalog {len(catalog)}."
            )
            picker_total = len(picker_nous.get("models", [])) + len(
                picker_nous.get("extra_models", [])
            )
            assert picker_total == len(catalog), (
                f"Picker featured + extras must equal live catalog size, "
                f"got {picker_total} vs {len(catalog)}."
            )

            # And they pick THE SAME featured set (not e.g. one's first-15
            # and another's last-15).
            card_ids = [m["id"] for m in card["models"]]
            picker_ids = [m["id"] for m in picker_nous["models"]]
            assert card_ids == picker_ids, (
                f"Providers card and picker must show the SAME featured "
                f"set so users see consistent labels in both places. "
                f"Card: {card_ids}\nPicker: {picker_ids}"
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 6 — Frontend contract (static-source assertions)
# ────────────────────────────────────────────────────────────────────────


class TestFrontendExtrasContract:
    """Pin the JS-side contract: dropdown reads `models`, slash command and
    label map ALSO read `extra_models`. Without this, a model from the
    catalog tail gets a bare-ID label or is invisible to /model autocomplete."""

    def test_ui_js_hydrates_dynamic_labels_from_extra_models(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(
            encoding="utf-8"
        )
        # Find the populateModelDropdown function and check it consumes
        # extra_models. Use a windowed substring search so the test stays
        # robust against minor refactors of surrounding code.
        idx = src.find("async function populateModelDropdown")
        assert idx != -1
        body = src[idx : idx + 3000]
        assert "extra_models" in body, (
            "populateModelDropdown must hydrate _dynamicModelLabels from "
            "g.extra_models so a model selected outside the featured set "
            "still gets a proper label. Without this, /model audio-lines "
            "→ 'audio-lines' bare-ID display. (#1567)"
        )

    def test_commands_js_loads_slash_args_from_extra_models(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "static" / "commands.js"
        ).read_text(encoding="utf-8")
        idx = src.find("async function _loadSlashModelSubArgs")
        assert idx != -1
        body = src[idx : idx + 1500]
        assert "extra_models" in body, (
            "_loadSlashModelSubArgs must iterate group.extra_models so /model "
            "autocomplete covers the full catalog, not just the dropdown's "
            "featured subset. The slash command exists precisely so power "
            "users can reach any model by typing its name. (#1567)"
        )

    def test_panels_js_uses_models_total_for_count(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "static" / "panels.js"
        ).read_text(encoding="utf-8")
        idx = src.find("function _buildProviderCard")
        assert idx != -1
        body = src[idx : idx + 1500]
        assert "models_total" in body, (
            "Provider card header should use p.models_total (full catalog "
            "size) for the count, not p.models.length (which is now the "
            "trimmed featured-set size). Without this, the header text says "
            "'15 models' instead of '396 models' for capped catalogs. (#1567)"
        )

    def test_panels_js_renders_more_disclosure_pill(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "static" / "panels.js"
        ).read_text(encoding="utf-8")
        # The "+N more" disclosure must reference the difference between
        # rendered count and total count somewhere in the providers-card
        # rendering path.
        assert "provider-card-model-tag-more" in src, (
            "Provider card must render a '+N more' disclosure pill when "
            "len(models) < models_total, so users know the dropdown is "
            "intentionally capped and the rest is reachable via /model."
        )
