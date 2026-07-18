"""Regression coverage for production turn-owned workspace artifacts."""

import json
from pathlib import Path

from api.artifact_references import derive_file_artifact_references


REPO = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _function_block(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    next_def = source.find("\n            def ", start + 1)
    assert next_def != -1, f"end of {name} not found"
    return source[start:next_def]


def test_successful_write_file_emits_only_bounded_reference_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "final.md"
    result = json.dumps({
        "bytes_written": 19,
        "resolved_path": str(target),
        "files_modified": [str(target)],
        "output": "private generated body",
    })

    references = derive_file_artifact_references(
        "write_file",
        {"path": str(target), "content": "private input body"},
        result,
        workspace,
        tool_call_id="call-write",
    )

    assert references == [{
        "kind": "workspace_file",
        "path": "reports/final.md",
        "source_tool": "write_file",
        "tool_call_id": "call-write",
    }]
    encoded = json.dumps(references)
    assert "private generated body" not in encoded
    assert "private input body" not in encoded
    assert str(workspace) not in encoded


def test_failed_incomplete_or_read_only_results_do_not_become_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = str(workspace / "failed.md")

    cases = [
        ("write_file", {"path": path}, json.dumps({"error": "permission denied"})),
        ("write_file", {"path": path}, json.dumps({"resolved_path": path})),
        ("patch", {"path": path}, json.dumps({"success": False, "error": "stale"})),
        ("patch", {"path": path}, "truncated result"),
        ("read_file", {"path": path}, json.dumps({"bytes_written": 1})),
        ("terminal", {"command": f"touch {path}"}, json.dumps({"success": True})),
        ("create_file", {"path": path}, json.dumps({"bytes_written": 1})),
    ]

    for name, args, result in cases:
        assert derive_file_artifact_references(
            name, args, result, workspace, tool_call_id="call-failed"
        ) == []
    assert derive_file_artifact_references(
        "write_file", {"path": path}, json.dumps({"bytes_written": 1}), ""
    ) == []


def test_successful_patch_keeps_result_order_dedupes_and_rejects_unsafe_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    cache_path = workspace / "node_modules" / "generated.js"
    root_build_path = workspace / "build" / "bundle.js"
    nested_build_path = workspace / "docs" / "build" / "report.md"
    first = workspace / "src" / "app.py"
    second = workspace / "Makefile"
    result = json.dumps({
        "success": True,
        "files_modified": [
            str(first),
            str(first),
            str(outside),
            "../traversal.md",
            "https://example.com/not-a-file",
            str(cache_path),
            str(root_build_path),
            str(nested_build_path),
            {"path": str(workspace / "not-a-string.md")},
            str(second),
        ],
    })

    references = derive_file_artifact_references(
        "patch", {"mode": "patch"}, result, workspace, tool_call_id="call-patch"
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "docs/build/report.md",
        "Makefile",
    ]


def test_successful_patch_can_fall_back_to_v4a_targets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = {
        "mode": "patch",
        "patch": "\n".join([
            "*** Begin Patch",
            "*** Update File: src/app.py",
            "*** Add File: Makefile",
            "*** Move File: old.txt -> docs/new.txt",
            "*** End Patch",
        ]),
    }

    references = derive_file_artifact_references(
        "functions.patch",
        args,
        json.dumps({"success": True}),
        workspace,
        tool_call_id="call-v4a",
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "Makefile",
        "old.txt",
        "docs/new.txt",
    ]


def test_existing_symlink_escape_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    link.symlink_to(outside, target_is_directory=True)

    references = derive_file_artifact_references(
        "write_file",
        {"path": "linked/secret.md"},
        json.dumps({"bytes_written": 1, "resolved_path": str(link / "secret.md")}),
        workspace,
        tool_call_id="call-link",
    )

    assert references == []


def test_foreign_drive_path_is_rejected_on_posix(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    references = derive_file_artifact_references(
        "write_file",
        {"path": r"C:\\foreign\\report.md"},
        json.dumps({"bytes_written": 1, "resolved_path": r"C:\\foreign\\report.md"}),
        workspace,
        tool_call_id="call-foreign-drive",
    )

    assert references == []


def test_backend_emits_artifact_after_tool_completion_and_keeps_cancel_journalable():
    modern = _function_block(STREAMING_PY, "on_tool_complete")
    legacy = _function_block(STREAMING_PY, "on_tool")

    for block in (modern, legacy):
        derive = block.index("derive_file_artifact_references(")
        tool_complete = block.index("put('tool_complete'")
        artifact = block.index("put('artifact_reference', artifact_reference)")
        assert derive < tool_complete < artifact

    assert "event not in ('cancel', 'error', 'artifact_reference')" in STREAMING_PY


def test_frontend_routes_artifact_sse_to_anchor_without_repainting_worklog():
    start = MESSAGES_JS.index("source.addEventListener('artifact_reference'")
    end = MESSAGES_JS.index("source.addEventListener('todo_state'", start)
    block = MESSAGES_JS[start:end]

    assert "S.session.session_id!==activeSid" in block
    assert "S.activeStreamId!==streamId" in block
    assert "_applyToAnchor('artifact_reference',d,e,null,{render:false});" in block
    assert "'tool_complete','artifact_reference','todo_state'" in MESSAGES_JS
