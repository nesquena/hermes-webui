"""Behavior regressions for actionable lazy media-preview HTTP errors."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_source(name: str) -> str:
    marker = f"function {name}("
    start = UI_JS.index(marker)
    brace = UI_JS.index("{", start)
    depth = 0
    quote = None
    escaped = False
    template_depth = 0
    for idx in range(brace, len(UI_JS)):
        char = UI_JS[idx]
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote and template_depth == 0:
                quote = None
            elif quote == "`" and char == "$" and idx + 1 < len(UI_JS) and UI_JS[idx + 1] == "{":
                template_depth += 1
            elif quote == "`" and char == "}" and template_depth:
                template_depth -= 1
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : idx + 1]
    raise AssertionError(f"unbalanced function: {name}")


def _section_source(name: str, next_name: str) -> str:
    start = UI_JS.index(f"function {name}(")
    end = UI_JS.index(f"function {next_name}(", start)
    return UI_JS[start:end]


def _run_node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_diff_preview_explains_forbidden_path_instead_of_calling_patch_broken():
    """The reported 403 shape must tell the user to move/copy the file."""
    require_response = _function_source("_requireMediaResponse")
    error_key = _function_source("_mediaPreviewErrorKey")
    load_diff = _function_source("loadDiffInline")
    script = f"""
const el={{dataset:{{path:'/outside/report.patch'}},setAttribute(){{}},outerHTML:''}};
const root={{querySelectorAll(){{return [el];}}}};
const translations={{
  diff_error:'Could not load patch file',
  media_preview_forbidden:'This file is outside WebUI allowed locations. Move or copy it into the active workspace, then try again.'
}};
const t=(key)=>translations[key]||key;
const esc=(value)=>String(value);
const fetch=()=>Promise.resolve({{ok:false,status:403}});
const _mediaSnapQuery=()=>'';
{require_response}
{error_key}
{load_diff}
loadDiffInline(root);
setTimeout(()=>process.stdout.write(JSON.stringify({{html:el.outerHTML}})),0);
"""
    result = _run_node(script)
    assert "Move or copy it into the active workspace" in result["html"]
    assert "Could not load patch file" not in result["html"]


def test_media_preview_http_statuses_share_one_actionable_classifier():
    require_response = _function_source("_requireMediaResponse")
    error_key = _function_source("_mediaPreviewErrorKey")
    result = _run_node(f"""
{require_response}
{error_key}
const statuses=[401,403,404,500];
const keys=statuses.map(status=>{{
  try{{_requireMediaResponse({{ok:false,status}});}}
  catch(error){{return _mediaPreviewErrorKey(error,'format_error');}}
}});
const networkKey=_mediaPreviewErrorKey(new TypeError('network'),'format_error');
const ok=_requireMediaResponse({{ok:true,status:200,marker:'same response'}}).marker;
process.stdout.write(JSON.stringify({{keys,networkKey,ok}}));
""")
    assert result == {
        "keys": [
            "media_preview_unauthorized",
            "media_preview_forbidden",
            "media_preview_not_found",
            "format_error",
        ],
        "networkKey": "format_error",
        "ok": "same response",
    }


def test_all_lazy_media_fetchers_use_the_shared_http_error_contract():
    fallbacks = {
        "loadDiffInline": ("buildCsvTablePreview", "diff_error"),
        "loadCsvInline": ("loadExcalidrawInline", "csv_error"),
        "loadExcalidrawInline": ("_renderExcalidrawCanvases", "excalidraw_error"),
        "loadPdfInline": ("loadHtmlInline", "pdf_error"),
        "loadHtmlInline": ("renderMermaidBlocks", "html_error"),
    }
    for function_name, (next_name, fallback) in fallbacks.items():
        body = _section_source(function_name, next_name)
        assert "_requireMediaResponse" in body, function_name
        assert f"_mediaPreviewErrorKey(error,'{fallback}')" in body, function_name
