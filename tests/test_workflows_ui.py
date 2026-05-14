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


def test_workflows_main_view_participates_in_panel_minimize_switching():
    compact_css = "".join(STYLE_CSS.split())
    assert "main.main>#mainWorkflows" in compact_css
    assert "not(.showing-workflows)" in STYLE_CSS
    assert "main.main.showing-workflows>#mainWorkflows{display:flex;}" in compact_css


def test_workflow_dag_nodes_are_opaque_over_graph_background():
    node_rule_start = STYLE_CSS.index(".workflow-dag-node{")
    node_rule = STYLE_CSS[node_rule_start : STYLE_CSS.index("}", node_rule_start)]
    assert "background:var(--surface);" in node_rule
    assert "transparent" not in node_rule


def test_workflow_dag_status_semantics_are_visible_for_all_canonical_states():
    for status in (
        "waiting",
        "ready",
        "running",
        "blocked",
        "failed",
        "review",
        "publish",
        "done",
        "cancelled",
    ):
        assert f".workflow-dag-node.status-{status}" in STYLE_CSS
    assert "workflow-status-dot" in PANELS_JS
    assert "workflow-dag-status" in PANELS_JS


def test_workflow_node_inspector_renders_canonical_fact_sections():
    for label in (
        "Summary",
        "Dependencies",
        "Definition of Done",
        "Execution",
        "Gate",
        "Evidence",
        "Artifacts",
        "Audit Events",
        "Insights",
    ):
        assert label in PANELS_JS
    for helper in (
        "_workflowNodeInspectorSection",
        "_workflowNodeList",
        "_workflowNodeEventRows",
        "_workflowNodeArtifactRows",
    ):
        assert helper in PANELS_JS
    assert "LLM insight" in PANELS_JS


def test_workflow_detail_renders_workflow_level_events_and_artifacts_panes():
    for label in (
        "Workflow Events",
        "Workflow Artifacts",
        "Loading workflow events",
        "Loading workflow artifacts",
    ):
        assert label in PANELS_JS
    for helper in (
        "loadWorkflowEvents",
        "loadWorkflowArtifacts",
        "_workflowEventRows",
        "_workflowArtifactRows",
    ):
        assert helper in PANELS_JS
    assert "workflowEventsPane" in PANELS_JS
    assert "workflowArtifactsPane" in PANELS_JS
    assert "`/api/workflows/${encodeURIComponent(workflowId)}/events`" in PANELS_JS
    assert "`/api/workflows/${encodeURIComponent(workflowId)}/artifacts`" in PANELS_JS


def test_workflow_detail_has_workflow_level_pane_styles():
    for selector in (
        ".workflow-detail-panes",
        ".workflow-events-pane",
        ".workflow-artifacts-pane",
    ):
        assert selector in STYLE_CSS


def test_workflow_refresh_reloads_selected_detail_panes():
    assert "async function refreshWorkflows" in PANELS_JS
    assert "await loadWorkflows(true)" in PANELS_JS
    assert "if(_currentWorkflowId) await loadWorkflowDag(_currentWorkflowId)" in PANELS_JS
    assert 'onclick="refreshWorkflows()"' in INDEX_HTML


def test_loading_workflow_dag_also_loads_workflow_level_panes():
    assert "loadWorkflowEvents(workflowId)" in PANELS_JS
    assert "loadWorkflowArtifacts(workflowId)" in PANELS_JS


def test_workflow_polling_runs_only_while_workflows_panel_visible():
    for symbol in (
        "_workflowListPollInterval",
        "_workflowDetailPollInterval",
        "function _workflowStartPolling",
        "function _workflowStopPolling",
        "function _syncWorkflowPolling",
    ):
        assert symbol in PANELS_JS
    assert "setInterval(refreshWorkflows,30000)" in PANELS_JS
    assert "setInterval(_refreshSelectedWorkflowDetail,10000)" in PANELS_JS
    assert "document.hidden" in PANELS_JS
    assert "_currentPanel !== 'workflows'" in PANELS_JS
    assert "clearInterval(_workflowListPollInterval)" in PANELS_JS
    assert "clearInterval(_workflowDetailPollInterval)" in PANELS_JS


def test_workflow_polling_is_synced_from_panel_switch_and_visibility_events():
    assert "_syncWorkflowPolling();" in PANELS_JS
    assert "document.addEventListener('visibilitychange',_syncWorkflowPolling)" in PANELS_JS
    assert "if (nextPanel === 'workflows') { await loadWorkflows(); loadWorkflowInbox(); }" in PANELS_JS


def test_workflow_inbox_intake_shell_lists_and_creates_raw_items():
    for symbol in (
        "workflowInboxList",
        "workflowInboxTitle",
        "workflowInboxBody",
        "loadWorkflowInbox",
        "renderWorkflowInbox",
        "createWorkflowInboxItem",
        "api('/api/workflows/inbox')",
        "method:'POST'",
        "Inbox",
        "Capture raw work",
    ):
        assert symbol in PANELS_JS or symbol in INDEX_HTML
    for selector in (
        ".workflow-inbox",
        ".workflow-inbox-item",
        ".workflow-inbox-form",
    ):
        assert selector in STYLE_CSS


def test_workflow_inbox_items_can_be_selected_and_triaged():
    for symbol in (
        "_currentWorkflowInboxItemId",
        "loadWorkflowInboxItem",
        "triageWorkflowInboxItem",
        "workflowInboxDetail",
        "workflowInboxClassification",
        "workflowInboxWorkspacePath",
        "workflowInboxAssignedWorkflowId",
        "`/api/workflows/inbox/${encodeURIComponent(itemId)}`",
        "method:'PATCH'",
        "Decomposition-worthy",
    ):
        assert symbol in PANELS_JS or symbol in INDEX_HTML
    for selector in (
        ".workflow-inbox-detail",
        ".workflow-inbox-triage-form",
    ):
        assert selector in STYLE_CSS


def test_workflow_refresh_reloads_inbox_items_too():
    assert "await loadWorkflowInbox(true)" in PANELS_JS
    assert "loadWorkflowInbox();" in PANELS_JS


def test_workflow_detail_can_materialize_dag_to_kanban():
    for symbol in (
        "materializeWorkflowToKanban",
        "Materialize to Kanban",
        "workflow-materialize-actions",
        "`/api/workflows/${encodeURIComponent(workflowId)}/materialize`",
        "method:'POST'",
        "actorId:'webui'",
    ):
        assert symbol in PANELS_JS
    assert ".workflow-materialize-actions" in STYLE_CSS


def test_workflow_unavailable_ui_surfaces_capability_reason_and_recovery_hint():
    for symbol in (
        "_workflowErrorDetails",
        "JSON.parse(err.body)",
        "workflow-unavailable-recovery",
        "workflow-unavailable-meta",
        "Reason",
        "Recovery",
        "Dashboard",
    ):
        assert symbol in PANELS_JS
    assert "refreshWorkflows()" in PANELS_JS
