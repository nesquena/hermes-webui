"""Model-picker caps: curated catalogs render in full, generic catalogs stay capped.

Policy (scope-separated by design):
- OpenRouter's curated list (51) renders in full via the curated caps.
- Nous' curated list (33) renders in full; the full Portal catalog (385+)
  samples to the 40-entry visible target via the Nous featured-set builder.
- Every other provider keeps the generic overflow caps (25 threshold /
  15 visible) so expanding curated catalogs can't balloon all pickers.

These tests lock the invariant on both production paths — the generic
overflow splitter (with provider-scoped caps) and the Nous-specific
featured-set builder — plus the sticky-selection promotion rule.
"""

from api.config import (
    _MODEL_PICKER_OVERFLOW_THRESHOLD,
    _MODEL_PICKER_VISIBLE_TARGET,
    _NOUS_FEATURED_TARGET,
    _NOUS_FEATURED_THRESHOLD,
    _OPENROUTER_CURATED_TARGET,
    _OPENROUTER_CURATED_THRESHOLD,
    _build_nous_featured_set,
    _picker_caps_for_provider,
    _split_picker_overflow_models,
)

OPENROUTER_CURATED_SIZE = 51
NOUS_CURATED_SIZE = 33
PORTAL_CATALOG_SIZE = 385
GENERIC_OVER_CAP = 60  # a non-curated catalog just above the generic cap
FREE_TIER_AUGMENT = 30  # _OPENROUTER_FREE_TIER_AUGMENT_CAP


def _model_rows(n: int) -> list[dict]:
    return [{"id": f"vendor/model-{i}"} for i in range(n)]


def _free_rows(n: int) -> list[dict]:
    return [{"id": f"vendor/free-{i}"} for i in range(n)]


def test_openrouter_caps_render_curated_in_full():
    # OpenRouter's production path: generic splitter + dynamic openrouter
    # caps. With no augmentation appended, the threshold is the curated
    # limit itself (100) — the 51-row curated list renders in full.
    threshold, target = _picker_caps_for_provider("openrouter")
    assert (threshold, target) == (_OPENROUTER_CURATED_THRESHOLD, _OPENROUTER_CURATED_TARGET)
    assert OPENROUTER_CURATED_SIZE <= threshold
    visible, extras = _split_picker_overflow_models(
        _model_rows(OPENROUTER_CURATED_SIZE),
        provider_id="openrouter",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == OPENROUTER_CURATED_SIZE
    assert extras == []


def test_openrouter_curated_at_limit_with_partial_augmentation_renders_full():
    # Exactly 100 curated + 5 actually-appended free rows: the dynamic
    # threshold is 100 + 5 = 105, and 105 ≤ 105 — full render.
    threshold, target = _picker_caps_for_provider("openrouter", free_augment_count=5)
    assert threshold == _OPENROUTER_CURATED_THRESHOLD + 5
    rows = _model_rows(100) + _free_rows(5)
    assert len(rows) == 105 <= threshold
    visible, extras = _split_picker_overflow_models(
        rows,
        provider_id="openrouter",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == 105
    assert extras == []


def test_openrouter_curated_over_limit_no_augmentation_samples():
    # 101 curated + 0 free must sample even though the combined list (101)
    # is well under any fixed 130: the threshold is exactly 100 + 0.
    threshold, target = _picker_caps_for_provider("openrouter", free_augment_count=0)
    assert threshold == _OPENROUTER_CURATED_THRESHOLD
    rows = _model_rows(101)
    assert len(rows) > threshold
    selected = rows[-1]["id"]
    visible, extras = _split_picker_overflow_models(
        rows,
        selected_model_id=selected,
        provider_id="openrouter",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == _OPENROUTER_CURATED_TARGET
    assert selected in [m["id"] for m in visible]
    all_ids = [m["id"] for m in visible] + [m["id"] for m in extras]
    assert sorted(all_ids) == sorted(m["id"] for m in rows)


def test_openrouter_curated_over_limit_with_partial_augmentation_samples():
    # 101 curated + 5 free: threshold 105, combined 106 > 105 → sampled.
    threshold, target = _picker_caps_for_provider("openrouter", free_augment_count=5)
    assert threshold == _OPENROUTER_CURATED_THRESHOLD + 5
    rows = _model_rows(101) + _free_rows(5)
    assert len(rows) > threshold
    selected = rows[-1]["id"]
    visible, extras = _split_picker_overflow_models(
        rows,
        selected_model_id=selected,
        provider_id="openrouter",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == _OPENROUTER_CURATED_TARGET
    assert selected in [m["id"] for m in visible]
    all_ids = [m["id"] for m in visible] + [m["id"] for m in extras]
    assert sorted(all_ids) == sorted(m["id"] for m in rows)


def test_openrouter_full_augmentation_budget_renders_curated_in_full():
    # 71 curated + full 30 free = 101 ≤ 130 (threshold 100+30) — full.
    threshold, target = _picker_caps_for_provider("openrouter", free_augment_count=FREE_TIER_AUGMENT)
    assert threshold == _OPENROUTER_CURATED_THRESHOLD + FREE_TIER_AUGMENT
    rows = _model_rows(71) + _free_rows(FREE_TIER_AUGMENT)
    assert len(rows) <= threshold
    visible, extras = _split_picker_overflow_models(
        rows,
        provider_id="openrouter",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == 101
    assert extras == []


def test_generic_caps_still_cap_non_curated_providers():
    # A non-curated provider with 60 models must NOT balloon to 60 rows.
    threshold, target = _picker_caps_for_provider("custom")
    assert (threshold, target) == (
        _MODEL_PICKER_OVERFLOW_THRESHOLD,
        _MODEL_PICKER_VISIBLE_TARGET,
    )
    visible, extras = _split_picker_overflow_models(
        _model_rows(GENERIC_OVER_CAP),
        provider_id="custom",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == _MODEL_PICKER_VISIBLE_TARGET
    assert len(extras) == GENERIC_OVER_CAP - len(visible)


def test_generic_cap_discontinuity_is_bounded():
    # 101-row generic catalog still samples to the visible target — the
    # curated threshold must not leak into generic providers.
    threshold, target = _picker_caps_for_provider("custom")
    visible, extras = _split_picker_overflow_models(
        _model_rows(101),
        provider_id="custom",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == _MODEL_PICKER_VISIBLE_TARGET
    assert len(extras) == 101 - len(visible)


def test_nous_curated_catalog_not_sampled():
    curated = [f"vendor/model-{i}" for i in range(NOUS_CURATED_SIZE)]
    assert len(curated) <= _NOUS_FEATURED_THRESHOLD
    featured, extras = _build_nous_featured_set(curated)
    assert featured == curated
    assert extras == []


def test_full_portal_catalog_sampled_to_target():
    big = [f"vendor/model-{i}" for i in range(PORTAL_CATALOG_SIZE)]
    featured, extras = _build_nous_featured_set(big)
    assert len(featured) == _NOUS_FEATURED_TARGET
    assert len(extras) == len(big) - len(featured)


def test_selected_overflow_model_promoted_without_dropping_entries():
    # Sticky selection: a selected model in the overflow tail must displace
    # the last visible row — total entries preserved, nothing dropped.
    threshold, target = _picker_caps_for_provider("custom")
    rows = _model_rows(GENERIC_OVER_CAP)
    selected = rows[-1]["id"]  # in the overflow tail
    visible, extras = _split_picker_overflow_models(
        rows,
        selected_model_id=selected,
        provider_id="custom",
        threshold=threshold,
        target=target,
    )
    assert len(visible) == _MODEL_PICKER_VISIBLE_TARGET
    assert len(extras) == GENERIC_OVER_CAP - len(visible)
    assert selected in [m["id"] for m in visible]
    all_ids = [m["id"] for m in visible] + [m["id"] for m in extras]
    assert sorted(all_ids) == sorted(m["id"] for m in rows)
