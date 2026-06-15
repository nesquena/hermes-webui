from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_static_model_health_metadata_and_renderer_exist():
    renderer = _function_body(PANELS_JS, "function _renderStaticModelHealthTable")

    assert "const STATIC_MODEL_HEALTH_ROWS = [" in PANELS_JS
    assert "inputCostPerM" in PANELS_JS
    assert "outputCostPerM" in PANELS_JS
    assert "replacement:" in PANELS_JS
    assert '<details class="insights-card insights-model-health-card">' in renderer
    assert "<summary>" in renderer
    assert "insights-model-health-table" in renderer
    assert "insights_model_health_cost_per_m" in renderer
    assert "insights_model_health_replacement" in renderer
    assert "insights_model_health_quality" not in renderer
    assert "insights_model_health_hallucination" not in renderer


def test_render_insights_includes_static_table_before_usage_models():
    render = _function_body(PANELS_JS, "function _renderInsights")

    assert "const modelHealthHtml = _renderStaticModelHealthTable();" in render
    assert "${modelHealthHtml}" in render
    assert render.index("${modelHealthHtml}") < render.index("insights-usage-grid")
    assert "modelsHtml" in render


def test_no_runtime_quality_or_hallucination_fetch_added():
    load = _function_body(PANELS_JS, "async function loadInsights")

    assert "quality" not in load.lower()
    assert "hallucination" not in load.lower()
    assert "/api/model" not in load
    assert "api(`/api/insights?days=${period}`)" in load


def test_model_health_i18n_keys_exist_in_locale_blocks():
    keys = [
        "insights_model_health_title",
        "insights_model_health_provider",
        "insights_model_health_replacement",
        "insights_model_health_cost_per_m",
    ]
    # Split i18n.js into per-locale blocks (top-level "  <code>: {" entries) and
    # assert EVERY locale carries all four keys — a `count >= N` check silently
    # tolerates a locale missing the keys (English fallback). #3634 pt gap.
    import re

    locale_starts = [
        (m.group(1), m.start())
        for m in re.finditer(r"""^  ['"]?([A-Za-z_-]+)['"]?: \{$""", I18N_JS, re.MULTILINE)
    ]
    assert len(locale_starts) >= 13, f"expected 13+ locale blocks, found {len(locale_starts)}"
    bounds = locale_starts + [("__end__", len(I18N_JS))]
    for i, (code, start) in enumerate(locale_starts):
        block = I18N_JS[start : bounds[i + 1][1]]
        for key in keys:
            assert f"{key}:" in block, f"locale '{code}' is missing {key}"


def test_model_health_table_css_is_responsive_and_contained():
    assert ".insights-model-health-table" in STYLE_CSS
    assert ".insights-model-health-table .insights-table-head" in STYLE_CSS
    assert ".insights-model-health-card>summary" in STYLE_CSS
    assert "minmax(130px,1.5fr)" in STYLE_CSS
    assert "overflow-x:auto" in STYLE_CSS
    assert ".insights-model-health-replacement" in STYLE_CSS
