"""Source-inspection and behavioral tests for Kanban modal fields added in #4470."""
from __future__ import annotations
import json
import re
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
KANBAN_BRIDGE = (ROOT / "api" / "kanban_bridge.py").read_text(encoding="utf-8")

NODE = shutil.which("node")


def test_modal_html_exposes_new_fields():
    for field_id in (
        "kanbanTaskModalSkills",
        "kanbanTaskModalMaxRuntimeSeconds",
        "kanbanTaskModalParents",
        "kanbanTaskModalParentsList",
        "kanbanTaskModalWorkspacePathList",
        "kanbanTaskModalPriorityHint",
    ):
        assert f'id="{field_id}"' in INDEX_HTML, f"Missing: {field_id}"
    assert 'list="kanbanTaskModalParentsList"' in INDEX_HTML
    assert 'list="kanbanTaskModalWorkspacePathList"' in INDEX_HTML
    assert 'data-i18n="kanban_priority_hint"' in INDEX_HTML


def _submit_body():
    m = re.search(
        r"async function submitKanbanTaskModal\(\)\s*\{(.*?)\n\}",
        PANELS_JS, re.DOTALL,
    )
    assert m, "submitKanbanTaskModal() not found"
    return m.group(1)


def test_submit_reads_and_sends_skills_as_array():
    body = _submit_body()
    assert "kanbanTaskModalSkills" in body
    assert "payload.skills" in body
    assert ".split(',')" in body or ".split(', ')" in body


def test_submit_reads_and_sends_max_runtime_as_integer():
    body = _submit_body()
    assert "kanbanTaskModalMaxRuntimeSeconds" in body
    assert "payload.max_runtime_seconds" in body
    assert "/^[1-9]\\d*$/" in body
    assert "Number(maxRuntimeRaw)" in body
    assert "kanban_max_runtime_invalid" in body


def test_submit_reads_and_sends_parents_as_list():
    body = _submit_body()
    assert "kanbanTaskModalParents" in body
    assert "payload.parents" in body
    assert "[parentsRaw]" in body or "payload.parents = [" in body


def test_new_fields_only_in_create_branch():
    body = _submit_body()
    edit_match = re.search(r"if \(isEdit\) \{(.*?)\} else \{", body, re.DOTALL)
    assert edit_match, "isEdit...else block not found"
    edit_branch = edit_match.group(1)
    assert "payload.skills" not in edit_branch
    assert "payload.max_runtime_seconds" not in edit_branch
    assert "payload.parents" not in edit_branch


def test_reset_handler_covers_new_fields():
    m = re.search(
        r"function _kanbanResetTaskModalFields\([^)]*\)\s*\{(.*?)\n\}",
        PANELS_JS, re.DOTALL,
    )
    assert m, "_kanbanResetTaskModalFields() not found"
    body = m.group(1)
    assert "kanbanTaskModalSkills" in body
    assert "kanbanTaskModalMaxRuntimeSeconds" in body
    assert "kanbanTaskModalParents" in body


def test_labels_handler_disables_new_fields_in_edit():
    m = re.search(
        r"function _kanbanSetTaskModalLabels\([^)]*\)\s*\{(.*?)\n\}",
        PANELS_JS, re.DOTALL,
    )
    assert m, "_kanbanSetTaskModalLabels() not found"
    body = m.group(1)
    assert "kanbanTaskModalSkills" in body
    assert "kanbanTaskModalMaxRuntimeSeconds" in body
    assert "kanbanTaskModalParents" in body
    assert "disabled" in body


def test_datalist_helpers_exist_and_are_called():
    fn_wp = "function _kanbanPopulateWorkspacePathDatalist"
    fn_par = "function _kanbanPopulateParentsDatalist"
    assert fn_wp in PANELS_JS
    assert "kanbanTaskModalWorkspacePathList" in PANELS_JS
    # Check that workspace_path appears inside the function definition body
    fn_start = PANELS_JS.find(fn_wp)
    assert "workspace_path" in PANELS_JS[fn_start:fn_start + 500], \
        "workspace_path not found in _kanbanPopulateWorkspacePathDatalist body"
    assert fn_par in PANELS_JS
    assert "_kanbanLinkableTaskOptions(null)" in PANELS_JS

    m = re.search(r"function openKanbanCreate\(\)\s*\{(.*?)\n\}", PANELS_JS, re.DOTALL)
    assert m, "openKanbanCreate() not found"
    body = m.group(1)
    assert "_kanbanPopulateWorkspacePathDatalist()" in body
    assert "_kanbanPopulateParentsDatalist()" in body


def test_i18n_new_keys_in_all_locales():
    required = [
        "kanban_priority_hint",
        "kanban_skills",
        "kanban_skills_placeholder",
        "kanban_max_runtime_seconds",
        "kanban_max_runtime_hint",
        "kanban_max_runtime_invalid",
        "kanban_parents_modal",
        "kanban_parents_placeholder",
    ]
    for key in required:
        count = I18N_JS.count(f"{key}:")
        assert count == 15, f"Key '{key}' appears {count} times, expected 15 (one per locale)"


def test_backend_already_accepts_new_fields():
    assert "skills=body.get(" in KANBAN_BRIDGE
    assert "max_runtime_seconds=body.get(" in KANBAN_BRIDGE
    assert "parents=body.get(" in KANBAN_BRIDGE


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_submit_payload_behavioral_and_validation():
    """Node.js execution: extract submitKanbanTaskModal and verify payload construction."""
    fn_start = PANELS_JS.find("async function submitKanbanTaskModal(")
    assert fn_start >= 0, "submitKanbanTaskModal not found"
    brace_start = PANELS_JS.index("{", fn_start)
    depth = 0
    fn_end = brace_start
    for i in range(brace_start, len(PANELS_JS)):
        if PANELS_JS[i] == "{":
            depth += 1
        elif PANELS_JS[i] == "}":
            depth -= 1
            if depth == 0:
                fn_end = i + 1
                break
    fn_body = PANELS_JS[fn_start:fn_end]

    harness = (
        "// Stub minimal DOM\n"
        "let lastFocused = null;\n"
        "function field(id, value = '') {\n"
        "  return { value, disabled: false, focus() { lastFocused = id; } };\n"
        "}\n"
        "const elements = {\n"
        "  kanbanTaskModalTitleInput: field('kanbanTaskModalTitleInput', 'Test Task'),\n"
        "  kanbanTaskModalBody: field('kanbanTaskModalBody', ''),\n"
        "  kanbanTaskModalStatus: field('kanbanTaskModalStatus', 'triage'),\n"
        "  kanbanTaskModalAssignee: field('kanbanTaskModalAssignee', 'agent1'),\n"
        "  kanbanTaskModalTenant: field('kanbanTaskModalTenant', ''),\n"
        "  kanbanTaskModalPriority: field('kanbanTaskModalPriority', '0'),\n"
        "  kanbanTaskModalWorkspaceKind: field('kanbanTaskModalWorkspaceKind', 'scratch'),\n"
        "  kanbanTaskModalWorkspacePath: field('kanbanTaskModalWorkspacePath', ''),\n"
        "  kanbanTaskModalSkills: field('kanbanTaskModalSkills', 'python, git'),\n"
        "  kanbanTaskModalMaxRuntimeSeconds: field('kanbanTaskModalMaxRuntimeSeconds', '120'),\n"
        "  kanbanTaskModalParents: field('kanbanTaskModalParents', 't_abc'),\n"
        "  kanbanTaskModalError: { textContent: '', dataset: {} },\n"
        "  kanbanTaskModalSubmit: { disabled: false },\n"
        "};\n"
        "global.document = { getElementById: (id) => elements[id] || null };\n"
        "function t(k) { return k; }\n"
        "let capturedPayload = null;\n"
        "async function api(url, opts) {\n"
        "  capturedPayload = opts ? JSON.parse(opts.body) : null;\n"
        "  return { task: { id: 't_new' } };\n"
        "}\n"
        "async function loadKanban() {}\n"
        "async function loadKanbanTask() {}\n"
        "function _kanbanBoardQuery() { return ''; }\n"
        "function closeKanbanTaskModal() {}\n"
        "let _kanbanTaskModalMode = 'create';\n"
        "let _kanbanTaskModalEditingId = null;\n"
        "let _kanbanTaskModalInitialDisplayedStatus = null;\n"
        + fn_body + "\n"
        "function resetForCase(maxRuntimeValue) {\n"
        "  capturedPayload = null;\n"
        "  lastFocused = null;\n"
        "  elements.kanbanTaskModalError.textContent = '';\n"
        "  delete elements.kanbanTaskModalError.dataset.warningShown;\n"
        "  elements.kanbanTaskModalSubmit.disabled = false;\n"
        "  elements.kanbanTaskModalTitleInput.value = 'Test Task';\n"
        "  elements.kanbanTaskModalBody.value = '';\n"
        "  elements.kanbanTaskModalStatus.value = 'triage';\n"
        "  elements.kanbanTaskModalAssignee.value = 'agent1';\n"
        "  elements.kanbanTaskModalTenant.value = '';\n"
        "  elements.kanbanTaskModalPriority.value = '0';\n"
        "  elements.kanbanTaskModalWorkspaceKind.value = 'scratch';\n"
        "  elements.kanbanTaskModalWorkspacePath.value = '';\n"
        "  elements.kanbanTaskModalSkills.value = 'python, git';\n"
        "  elements.kanbanTaskModalMaxRuntimeSeconds.value = maxRuntimeValue;\n"
        "  elements.kanbanTaskModalParents.value = 't_abc';\n"
        "}\n"
        "async function runCase(maxRuntimeValue) {\n"
        "  resetForCase(maxRuntimeValue);\n"
        "  await submitKanbanTaskModal();\n"
        "  return {\n"
        "    payload: capturedPayload,\n"
        "    error: elements.kanbanTaskModalError.textContent,\n"
        "    focused: lastFocused,\n"
        "    submitDisabled: elements.kanbanTaskModalSubmit.disabled,\n"
        "  };\n"
        "}\n"
        "(async () => {\n"
        "  const results = [];\n"
        "  for (const value of ['120', '', '0', '-5', '1.5', '1e2', 'abc']) {\n"
        "    results.push(await runCase(value));\n"
        "  }\n"
        "  console.log(JSON.stringify(results));\n"
        "})().catch(e => { process.stderr.write(String(e)); process.exit(1); });\n"
    )

    result = subprocess.run(
        [NODE, "-e", harness],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"Node error: {result.stderr}"
    output = result.stdout.strip()
    assert output, f"No payload captured. stderr: {result.stderr}"
    results = json.loads(output)

    valid, empty, zero, negative, decimal, scientific, alpha = results
    valid_payload = valid["payload"]
    assert valid_payload.get("skills") == ["python", "git"], f"skills wrong: {valid_payload.get('skills')}"
    assert valid_payload.get("max_runtime_seconds") == 120, f"max_runtime wrong: {valid_payload.get('max_runtime_seconds')}"
    assert valid_payload.get("parents") == ["t_abc"], f"parents wrong: {valid_payload.get('parents')}"
    assert valid["error"] == ""
    assert valid["focused"] is None

    empty_payload = empty["payload"]
    assert "max_runtime_seconds" not in empty_payload, f"empty max runtime should be omitted: {empty_payload}"
    assert empty["error"] == ""
    assert empty["focused"] is None

    for invalid_case in (zero, negative, decimal, scientific, alpha):
        assert invalid_case["payload"] is None, f"invalid runtime should not submit: {invalid_case}"
        assert invalid_case["error"] == "kanban_max_runtime_invalid", f"wrong validation message: {invalid_case}"
        assert invalid_case["focused"] == "kanbanTaskModalMaxRuntimeSeconds", f"wrong focus target: {invalid_case}"
        assert invalid_case["submitDisabled"] is False, f"submit button should stay enabled: {invalid_case}"
