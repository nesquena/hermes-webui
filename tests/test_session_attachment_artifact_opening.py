import json
import pathlib
import subprocess
import textwrap


REPO = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    for marker in (f"function {name}(", f"async function {name}("):
        start = src.find(marker)
        if start != -1:
            break
    else:
        raise AssertionError(f"could not find {name}()")
    brace = src.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(src):
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"could not extract {name}()"
    return src[start:pos]


def _run_attachment_resolver(path: str, session_id: str = "37023dbd9641"):
    js = textwrap.dedent(
        """
        const S={session:{session_id:process.argv[2]}};
        """
    )
    js += "\n" + _extract_function(WORKSPACE_JS, "_sessionAttachmentRootAbsolute")
    js += "\n" + _extract_function(WORKSPACE_JS, "_resolveArtifactOpenPath")
    js += textwrap.dedent(
        """
        const result=_resolveArtifactOpenPath(process.argv[1]);
        process.stdout.write(JSON.stringify(result));
        """
    )
    proc = subprocess.run(
        ["node", "-e", js, path, session_id],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


def test_attachment_artifact_paths_resolve_relative_to_current_session_inbox():
    result = _run_attachment_resolver(
        "/root/.hermes/webui/attachments/37023dbd9641/SOW_DreamIT_redline_jamie_rev.docx"
    )

    assert result == {
        "rel": "SOW_DreamIT_redline_jamie_rev.docx",
        "isAttachment": True,
        "absolutePath": "/root/.hermes/webui/attachments/37023dbd9641/SOW_DreamIT_redline_jamie_rev.docx",
    }


def test_non_attachment_artifact_paths_keep_workspace_relative_shape():
    result = _run_attachment_resolver("docs/report.pdf")

    assert result == {
        "rel": "docs/report.pdf",
        "isAttachment": False,
        "absolutePath": "docs/report.pdf",
    }


def test_open_artifact_path_skips_workspace_existence_gate_for_session_attachments():
    compact = "".join(WORKSPACE_JS.split())

    assert "constresolved=_resolveArtifactOpenPath(path);" in compact
    assert "if(!resolved.isAttachment&&!(await_workspacePathExists(rel)))" in compact
    assert "openFile(rel);" in compact
