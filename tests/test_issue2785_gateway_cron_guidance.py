"""Coverage for cron/gateway guidance in the Tasks panel and Docker docs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
PANELS_JS = ROOT / "static" / "panels.js"
DOCKER_DOC = ROOT / "docs" / "docker.md"


def test_tasks_panel_has_gateway_notice_container():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="cronGatewayNotice"' in html
    assert "detail-alert" in html


def test_cron_panel_loads_gateway_status_for_scheduling_guidance():
    panels = PANELS_JS.read_text(encoding="utf-8")

    assert "function _cronGatewayNoticeHtml" in panels
    assert "function loadCronGatewayNotice" in panels
    assert "api('/api/gateway/status')" in panels
    assert "Gateway not configured" in panels
    assert "Gateway not running" in panels
    assert "Gateway endpoint not reachable" in panels
    assert "configured gateway URL env var" in panels
    assert "GATEWAY_HEALTH_URL" in panels
    assert "scheduled jobs require the Hermes gateway daemon" in panels
    assert "loadCronGatewayNotice()" in panels


def test_docker_docs_explain_single_container_cron_gateway_boundary():
    docs = DOCKER_DOC.read_text(encoding="utf-8")

    assert "single-container setup runs the WebUI only" in docs
    assert "scheduled jobs require the Hermes gateway daemon" in docs
    assert "Gateway not configured" in docs
    assert "Gateway metadata stale" in docs
    assert "Gateway endpoint not reachable" in docs
    assert "`gateway_state.json` can become stale" in docs
    assert "HERMES_WEBUI_GATEWAY_BASE_URL" in docs
    assert "docker-compose.two-container.yml" in docs
