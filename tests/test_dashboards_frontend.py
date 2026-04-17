"""
Frontend contract tests for sections 5-6 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md.

These check the static wiring that ties HTML ↔ JS ↔ CSS ↔ i18n together.
No headless browser — grep-level assertions only.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
STATIC = REPO_ROOT / "static"


def _read(relpath):
    return (STATIC / relpath).read_text(encoding="utf-8")


# ── Section 5.1: sidebar nav-tabs + panel-view shells ──────────────────────

def test_index_html_has_three_new_nav_tabs():
    html = _read("index.html")
    for panel in ("insights", "surfaces", "pixel"):
        assert f'data-panel="{panel}"' in html, f"missing nav-tab for {panel}"
        assert f"switchPanel('{panel}')" in html, f"missing onclick for {panel}"


def test_index_html_has_three_panel_view_shells():
    html = _read("index.html")
    for panel in ("Insights", "Surfaces", "Pixel"):
        assert f'id="panel{panel}"' in html, f"missing sidebar panel-view #panel{panel}"


def test_index_html_has_three_main_dashboard_containers():
    html = _read("index.html")
    for panel in ("Insights", "Surfaces", "Pixel"):
        assert f'id="main{panel}"' in html, f"missing main dashboard container #main{panel}"


def test_index_html_loads_insights_and_surfaces_scripts():
    html = _read("index.html")
    assert "static/insights.js" in html
    assert "static/surfaces.js" in html


# ── Section 5.2: i18n keys across all four languages ──────────────────────

I18N_KEYS = [
    "tab_insights", "tab_surfaces", "tab_pixel",
    "insights_title", "insights_empty",
    "granularity_day", "granularity_week", "granularity_month",
    "refresh", "surfaces_title", "pixel_title", "back",
]

LOCALE_MARKERS = {
    "en":       "tab_chat: 'Chat',",
    "zh":       "tab_chat: '\u804a\u5929',",
    "de":       "tab_chat: 'Chat',\n    tab_tasks: 'Aufgaben'",
    "zh-Hant":  "'zh-Hant':",
}


def test_i18n_new_keys_present_in_all_locales():
    src = _read("i18n.js")
    # Split the file into rough locale chunks by the known section markers.
    # We just assert every key appears at least 4 times (one per locale).
    for key in I18N_KEYS:
        count = len(re.findall(rf"\b{re.escape(key)}\s*:", src))
        assert count >= 4, f"i18n key {key!r} only appears {count}× (expected ≥4 for en/zh/de/zh-Hant)"


# ── Section 5.3: panels.js switchPanel routing ────────────────────────────

def test_panels_js_routes_three_new_panels_in_switchPanel():
    src = _read("panels.js")
    for panel in ("insights", "surfaces", "pixel"):
        assert f"name === '{panel}'" in src, f"switchPanel missing branch for {panel}"


def test_panels_js_main_view_toggle_covers_dashboard_panels():
    src = _read("panels.js")
    # Both the dashboard list and the hide/show behaviour exist
    assert "_DASHBOARD_PANELS" in src
    for panel in ("insights", "surfaces", "pixel"):
        assert f"'{panel}'" in src


# ── Section 5.4: stylesheet additions ─────────────────────────────────────

def test_style_css_has_dashboard_classes():
    css = _read("style.css")
    for cls in (".dashboard-main", ".insights-card", ".insights-chart",
                ".insights-heatmap", ".surfaces-grid", ".surface-card",
                ".surface-state-light"):
        assert cls in css, f"CSS class {cls} missing"


def test_style_css_mobile_query_for_surfaces():
    css = _read("style.css")
    assert "max-width: 640px" in css
    assert ".surfaces-grid { grid-template-columns: 1fr" in css


# ── Section 6.x: insights.js contracts ────────────────────────────────────

def test_insights_js_calls_all_five_stats_endpoints():
    src = _read("insights.js")
    for endpoint in ("/api/stats/summary", "/api/stats/timeseries",
                     "/api/stats/response-time", "/api/stats/heatmap",
                     "/api/stats/models"):
        assert endpoint in src, f"insights.js missing call to {endpoint}"


def test_insights_js_supports_refresh_bypass():
    src = _read("insights.js")
    # Must add refresh=1 query param in the _get helper
    assert "refresh=1" in src


def test_insights_js_renders_both_total_and_split_timeseries():
    src = _read("insights.js")
    assert "source=total" in src
    assert "source=split" in src


def test_insights_js_has_empty_state():
    src = _read("insights.js")
    assert "insights_empty" in src


def test_insights_js_has_public_entrypoints():
    src = _read("insights.js")
    for name in ("showInsights", "refreshInsights",
                 "setInsightsGranularity", "setInsightsResponseWindow"):
        assert f"window.{name} =" in src or f"window.{name}=" in src, f"missing {name}"


# ── Surfaces stub (section 7 preview) ─────────────────────────────────────

def test_surfaces_js_has_icon_dictionary():
    src = _read("surfaces.js")
    for src_key in ("cli", "webui", "weixin", "telegram", "discord",
                    "slack", "signal", "whatsapp", "sms", "email", "cron"):
        assert f"{src_key}:" in src, f"surfaces.js missing icon for '{src_key}'"


def test_surfaces_js_webui_only_active_sessions():
    src = _read("surfaces.js")
    # Gated by source === 'webui'
    assert "s.source === 'webui'" in src
    assert "active_webui_sessions" in src


# ── Section 3.10 lint (already existed; keep it here with a tighter check) ─

def test_no_runtime_perception_phrasing_in_new_frontend_files():
    banned = (
        "currently running",
        "waiting for your reply",
        "agent is running",
        "is running tool",
    )
    for fname in ("insights.js", "surfaces.js"):
        text = _read(fname).lower()
        for phrase in banned:
            assert phrase not in text, f"banned phrase {phrase!r} appears in {fname}"
