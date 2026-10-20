"""Regression check for #7228 — model-picker search vs OpenRouter display names.

Composer model-picker search was a literal substring on the rendered name and
id. OpenRouter overflow rows were labeled with the raw id (``stealth/ox-alpha``)
instead of the provider display name (``Ox Alpha``), and spaces were not
normalized to hyphens — so typing the name users see in Hermes Desktop
(``Ox Alpha``) always yielded "No models found".

Fixed in three layers (issue requirement):
  1. backend: /api/models ships the friendly display name from the local
     OpenRouter metadata disk cache for curated catalog rows;
  2. frontend: getModelLabel() resolves via the dynamic label map, which is
     now hydrated with the display name;
  3. frontend: search folds whitespace/hyphens/dots on both sides so
     ``ox alpha`` == ``ox-alpha`` == ``ox.alpha``.

Verified at the source level so this stays fast.
"""
from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")


def test_backend_has_openrouter_display_name_helper():
    # The helper must read only the local disk cache (no network).
    assert "def _openrouter_model_display_name(model_id: str) -> str:" in CONFIG_PY
    assert "_load_model_metadata_disk_cache" in CONFIG_PY


def test_backend_ships_display_name_not_raw_id():
    # Curated OpenRouter catalog rows must use the friendly display name
    # instead of the raw id as the picker label (#7228).
    snippet = CONFIG_PY[CONFIG_PY.index('fetch_openrouter_models as _fetch_or_models'):]
    snippet = snippet[:snippet.index('except Exception')]
    assert "_openrouter_model_display_name(mid)" in snippet
    assert '{"id": mid, "label": mid}' not in snippet


def test_filter_models_folds_space_hyphen_dot():
    # Search must fold whitespace/hyphens/dots on both sides so display names
    # with spaces match ids with hyphens ("ox alpha" == "ox-alpha").
    assert "replace(/[\\s._-]+/g,'')" in UI_JS
    assert "_foldModelSearch(name).includes(foldTerm)" in UI_JS
    assert "_foldModelSearch(id).includes(foldTerm)" in UI_JS


def test_get_model_label_uses_dynamic_map_first():
    # getModelLabel() must resolve through the dynamic label map, which is
    # hydrated from m.label / overflowModel.label (now the display name).
    idx = UI_JS.index("function getModelLabel(modelId){")
    # The dynamic-map lookup must appear before the static-label fallback,
    # so backend display names win over the hardcoded table.
    assert "_dynamicModelLabels[modelId]" in UI_JS[idx:idx + 900]
    assert UI_JS[idx:idx + 900].index("_dynamicModelLabels[modelId]") < \
        (UI_JS[idx:idx + 900].index("STATIC_LABELS") if "STATIC_LABELS" in UI_JS[idx:idx + 900] else 10**9)
    # The dynamic map must be populated from the backend label, not the raw id.
    assert "_dynamicModelLabels[m.id]=m.label||m.id" in UI_JS


def test_literal_substring_match_still_present():
    # Raw-id search ("stealth", "stealth/ox-alpha") must keep working.
    assert "name.includes(term)||id.includes(term)" in UI_JS
