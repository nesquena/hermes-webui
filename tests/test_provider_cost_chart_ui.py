from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_provider_cost_chart_ui_guards_are_present():
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    # function is defined
    assert "async function renderProviderCostChart(card)" in panels_js

    # function is wired up inside loadProvidersPanel (fire-and-forget)
    assert "renderProviderCostChart(quotaCard)" in panels_js

    # fetch target is correct
    assert "/api/provider/cost-history?provider=openrouter" in panels_js

    # CSS container class present in both JS and CSS
    assert "provider-cost-chart-wrap" in panels_js
    assert "provider-cost-chart-wrap" in style_css

    # monthly pace projection annotation
    assert "Monthly pace" in panels_js

    # null delta guard for the oldest snapshot
    assert "s.delta!=null" in panels_js
