from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
CONTROL_PLANE_PY = (REPO / "api" / "control_plane.py").read_text(encoding="utf-8")


def test_control_plane_has_desktop_and_mobile_nav_entries():
    assert 'data-panel="controlPlane"' in INDEX_HTML
    assert 'data-i18n-title="tab_control_plane"' in INDEX_HTML
    assert 'id="panelControlPlane"' in INDEX_HTML


def test_control_plane_route_serves_spa_after_auth():
    routes_py = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/control-plane"' in routes_py
    assert '/api/control-plane/morning-brief-canary' in CONTROL_PLANE_PY


def test_control_plane_panel_loads_read_only_api_and_redacted_target_fields():
    assert 'async function loadControlPlane(' in PANELS_JS
    assert "api('/api/control-plane/morning-brief-canary')" in PANELS_JS
    assert "api('/api/control-plane/overview')" in PANELS_JS
    assert "api('/health')" in PANELS_JS
    assert 'target_label' in PANELS_JS
    assert 'target_class' in PANELS_JS
    assert 'telegram://dry-run/' not in PANELS_JS


def test_control_plane_expanded_reader_cards_are_present():
    expected_cards = [
        'Routine / Cron health',
        'NotebookLM pre-warm',
        'Supabase control-plane canary',
        'Approval gates / HOLD list',
        'Recent artifacts / verification reports',
        'WebUI health',
    ]
    for card in expected_cards:
        assert card in CONTROL_PLANE_PY
    assert '/api/control-plane/overview' in CONTROL_PLANE_PY


def test_control_plane_direct_url_switches_to_panel():
    assert "location.pathname==='/control-plane'" in PANELS_JS
    assert "switchPanel('controlPlane'" in PANELS_JS
