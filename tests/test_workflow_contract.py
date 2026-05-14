import json
from pathlib import Path



def test_webui_loads_core_workflow_api_contract_fixture():
    from api.workflow_contract import (
        WORKFLOW_API_CONTRACT_VERSION,
        load_workflow_api_contract_fixture,
        workflow_contract_fixture_path,
    )

    path = workflow_contract_fixture_path()
    fixture = load_workflow_api_contract_fixture()

    assert path.name == "workflow-api-v1.fixture.json"
    assert path.exists()
    assert WORKFLOW_API_CONTRACT_VERSION == "workflow-api-v1"
    assert fixture["contractVersion"] == WORKFLOW_API_CONTRACT_VERSION
    assert fixture["envelope"] == {"facts": {}, "insights": None}

    fixtures = fixture["fixtures"]
    assert {"workflowList", "workflowDag", "workflowNode", "workflowEvents", "workflowArtifacts", "inboxList", "inboxItem", "inboxShape", "inboxPromote"} == set(fixtures)
    dag_facts = fixtures["workflowDag"]["facts"]
    assert {"workflow", "nodes", "edges", "gates", "artifacts", "controlActions"}.issubset(dag_facts)
    assert dag_facts["workflow"]["workspacePath"] == "/tmp/workflow-contract"
    assert dag_facts["nodes"][0]["workspace"]["worktreePath"] == "/tmp/worktrees/wf_contract-shape-plan"
    assert dag_facts["controlActions"][0]["endpoint"] == "/api/workflows/wf_contract/gates/gate_contract_review/resolve"
    assert fixtures["inboxShape"]["facts"]["draftWorkflow"]["sourceInboxItemId"] == "inbox_contract"
    assert fixtures["inboxPromote"]["facts"]["dag"]["workflow_id"] == "wf_contract"


def test_workflow_contract_fixture_matches_ui_field_names():
    from api.workflow_contract import load_workflow_api_contract_fixture

    fixture = load_workflow_api_contract_fixture()
    workflow = fixture["fixtures"]["workflowDag"]["facts"]["workflow"]
    node = fixture["fixtures"]["workflowDag"]["facts"]["nodes"][0]
    gate = fixture["fixtures"]["workflowDag"]["facts"]["gates"][0]
    artifact = fixture["fixtures"]["workflowDag"]["facts"]["artifacts"][0]
    inbox = fixture["fixtures"]["inboxItem"]["facts"]["inboxItem"]

    for key in ["workspacePath", "currentGate", "policyPath", "policySnapshot", "createdAt", "updatedAt", "createdBy"]:
        assert key in workflow
    for key in ["gateLevel", "gateType", "kanbanTaskId", "definitionOfDone", "createdAt", "updatedAt"]:
        assert key in node
    for key in ["workflowId", "nodeId", "gateType", "requiredActor", "resolvedBy", "resolvedAt", "artifactId"]:
        assert key in gate
    for key in ["workflowId", "mimeType", "schemaVersion", "createdAt", "createdBy"]:
        assert key in artifact
    for key in ["workspacePath", "assignedWorkflowId", "createdAt", "updatedAt", "createdBy"]:
        assert key in inbox


def test_workflow_contract_fixture_drift_check_reports_mismatch(tmp_path):
    from scripts.check_workflow_contract_fixture import check_fixture_sync

    core_fixture = {"contractVersion": "workflow-api-v1", "fixtures": {"workflowList": {"facts": {}}}}
    webui_fixture = {"contractVersion": "workflow-api-v1", "fixtures": {"workflowList": {"facts": {"extra": True}}}}
    core_path = tmp_path / "core.json"
    webui_path = tmp_path / "webui.json"
    core_path.write_text(json.dumps(core_fixture), encoding="utf-8")
    webui_path.write_text(json.dumps(webui_fixture), encoding="utf-8")

    result = check_fixture_sync(core_path, webui_path)

    assert result.ok is False
    assert result.core_path == core_path
    assert result.webui_path == webui_path
    assert "WebUI workflow contract fixture is stale" in result.message


def test_workflow_contract_fixture_drift_check_accepts_matching_json_with_different_formatting(tmp_path):
    from scripts.check_workflow_contract_fixture import check_fixture_sync

    fixture = {"contractVersion": "workflow-api-v1", "fixtures": {"workflowList": {"facts": {"count": 1}}}}
    core_path = tmp_path / "core.json"
    webui_path = tmp_path / "webui.json"
    core_path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    webui_path.write_text(json.dumps(fixture, separators=(",", ":")), encoding="utf-8")

    result = check_fixture_sync(core_path, webui_path)

    assert result.ok is True
    assert result.message == "Workflow contract fixtures are in sync."
