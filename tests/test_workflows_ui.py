import pathlib

REPO = pathlib.Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_workflows_navigation_and_panel_shell_are_present():
    assert 'data-panel="workflows"' in INDEX_HTML
    assert 'id="panelWorkflows"' in INDEX_HTML
    assert 'id="mainWorkflows"' in INDEX_HTML
    assert "Workflows" in INDEX_HTML


def test_workflows_panel_loads_via_shared_relative_api_helper():
    assert "async function loadWorkflows" in PANELS_JS
    assert "api('/api/workflows')" in PANELS_JS
    assert "fetch('/api/workflows" not in PANELS_JS
    assert "fetch(\"/api/workflows" not in PANELS_JS
    assert "nextPanel === 'workflows'" in PANELS_JS
    assert "'workflows'" in PANELS_JS


def test_workflows_shell_has_empty_unavailable_and_card_styles():
    for selector in (
        ".workflow-list",
        ".workflow-card",
        ".workflow-dag-shell",
        ".workflow-unavailable",
    ):
        assert selector in STYLE_CSS
    assert "No workflows yet." in PANELS_JS
    assert "Workflow API is not available" in PANELS_JS


def test_workflows_dag_canvas_and_node_inspector_are_present():
    assert "renderWorkflowDagCanvas" in PANELS_JS
    assert "workflow-dag-canvas" in PANELS_JS
    assert "workflow-dag-edge-layer" in PANELS_JS
    assert "workflow-dag-node" in PANELS_JS
    assert "workflow-node-inspector" in PANELS_JS
    assert "loadWorkflowNode" in PANELS_JS
    assert "/nodes/${encodeURIComponent(nodeId)}" in PANELS_JS
    for selector in (
        ".workflow-dag-layout",
        ".workflow-dag-canvas",
        ".workflow-dag-edge-layer",
        ".workflow-dag-node",
        ".workflow-node-inspector",
    ):
        assert selector in STYLE_CSS
