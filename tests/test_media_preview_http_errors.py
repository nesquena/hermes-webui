"""Behavior regressions for actionable lazy media-preview HTTP errors."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
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
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _downloadable_preview_harness(function_name: str) -> tuple[list[str], str]:
    next_functions = {
        "loadCsvInline": "loadExcalidrawInline",
        "loadPdfInline": "loadHtmlInline",
        "loadHtmlInline": "renderMermaidBlocks",
    }
    helpers = [
        _function_source("_requireMediaResponse"),
        _function_source("_mediaPreviewErrorKey"),
        _function_source("_mediaPreviewAllowsDownload"),
        _section_source("_mediaSnapQuery", "_csvMediaUrl"),
    ]
    setup = ""
    if function_name == "loadCsvInline":
        helpers.extend(
            [
                _section_source("_mediaSessionQuery", "_mediaSnapQuery"),
                _section_source("_csvMediaUrl", "buildCsvTablePreview"),
                _section_source("_csvPreviewErrorHtml", "loadCsvInline"),
            ]
        )
    elif function_name == "loadPdfInline":
        setup = "let _pdfjsReady=true,_pdfjsLoading=false;const window={_pdfjsLib:{}};"
    helpers.append(_section_source(function_name, next_functions[function_name]))
    return helpers, setup


def test_forbidden_copy_covers_location_and_permission_failures():
    script = (
        "const localStorage={getItem(){return null;},setItem(){}};\n"
        "const document={documentElement:{},querySelectorAll(){return [];}};\n"
        + I18N_JS
        + "\nprocess.stdout.write(JSON.stringify({message:t('media_preview_forbidden')}));"
    )
    result = _run_node(script)
    assert result["message"] == (
        "WebUI could not access this file. Move or copy it into the active workspace, "
        "or check file permissions, then try again."
    )


def test_diff_preview_explains_forbidden_access_instead_of_calling_patch_broken():
    """The reported 403 shape must provide accurate recovery options."""
    require_response = _function_source("_requireMediaResponse")
    error_key = _function_source("_mediaPreviewErrorKey")
    load_diff = _function_source("loadDiffInline")
    script = f"""
const el={{dataset:{{path:'/outside/report.patch'}},setAttribute(){{}},outerHTML:''}};
const root={{querySelectorAll(){{return [el];}}}};
const translations={{
  diff_error:'Could not load patch file',
  media_preview_forbidden:'WebUI could not access this file. Move or copy it into the active workspace, or check file permissions, then try again.'
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
    assert "WebUI could not access this file" in result["html"]
    assert "check file permissions" in result["html"]
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


def test_unavailable_media_download_policy_only_blocks_forbidden_and_missing():
    allows_download = _function_source("_mediaPreviewAllowsDownload")
    result = _run_node(f"""
{allows_download}
const keys=[
  'media_preview_unauthorized',
  'media_preview_forbidden',
  'media_preview_not_found',
  'format_error'
];
const allowed=keys.map(key=>_mediaPreviewAllowsDownload(key));
process.stdout.write(JSON.stringify({{allowed}}));
""")
    assert result == {"allowed": [True, False, False, True]}


@pytest.mark.parametrize(
    ("function_name", "path"),
    [
        ("loadDiffInline", "/workspace/report.patch"),
        ("loadCsvInline", "/workspace/report.csv"),
        ("loadExcalidrawInline", "/workspace/report.excalidraw"),
        ("loadPdfInline", "/workspace/report.pdf"),
        ("loadHtmlInline", "/workspace/report.html"),
    ],
)
def test_all_lazy_media_fetchers_render_the_shared_http_error_contract(
    function_name: str, path: str
):
    next_functions = {
        "loadDiffInline": "_mediaSessionQuery",
        "loadCsvInline": "loadExcalidrawInline",
        "loadExcalidrawInline": "_renderExcalidrawCanvases",
        "loadPdfInline": "loadHtmlInline",
        "loadHtmlInline": "renderMermaidBlocks",
    }
    helpers = [
        _function_source("_requireMediaResponse"),
        _function_source("_mediaPreviewErrorKey"),
        _function_source("_mediaPreviewAllowsDownload"),
        _section_source("_mediaSnapQuery", "_csvMediaUrl"),
    ]
    setup = ""
    if function_name == "loadCsvInline":
        helpers.extend(
            [
                _section_source("_mediaSessionQuery", "_mediaSnapQuery"),
                _section_source("_csvMediaUrl", "buildCsvTablePreview"),
                _section_source("_csvPreviewErrorHtml", "loadCsvInline"),
            ]
        )
    elif function_name == "loadPdfInline":
        setup = "let _pdfjsReady=true,_pdfjsLoading=false;const window={_pdfjsLib:{}};"

    helpers.append(_section_source(function_name, next_functions[function_name]))
    script = f"""
const el={{dataset:{{path:{json.dumps(path)}}},setAttribute(){{}},outerHTML:'',parentNode:{{}}}};
const root={{querySelectorAll(){{return [el];}}}};
const translations={{
  media_preview_forbidden:'WebUI could not access this file. Move or copy it into the active workspace, or check file permissions, then try again.',
  diff_error:'FORMAT ERROR',csv_error:'FORMAT ERROR',excalidraw_error:'FORMAT ERROR',
  pdf_error:'FORMAT ERROR',html_error:'FORMAT ERROR'
}};
const t=(key)=>translations[key]||key;
const esc=(value)=>String(value);
const fetch=()=>Promise.resolve({{ok:false,status:403}});
{setup}
{''.join(helpers)}
{function_name}(root);
setTimeout(()=>process.stdout.write(JSON.stringify({{html:el.outerHTML}})),0);
"""
    result = _run_node(script)
    assert "WebUI could not access this file" in result["html"]
    assert "check file permissions" in result["html"]
    assert "FORMAT ERROR" not in result["html"]
    if function_name in {"loadCsvInline", "loadPdfInline", "loadHtmlInline"}:
        assert "msg-media-link" not in result["html"]


@pytest.mark.parametrize(
    ("function_name", "extension"),
    [
        ("loadCsvInline", "csv"),
        ("loadPdfInline", "pdf"),
        ("loadHtmlInline", "html"),
    ],
)
@pytest.mark.parametrize(
    ("failure", "expected_message", "should_link"),
    [
        ("403", "FORBIDDEN", False),
        ("404", "NOT FOUND", False),
        ("401", "UNAUTHORIZED", True),
        ("network", "FORMAT ERROR", True),
    ],
)
def test_downloadable_media_error_links_match_recoverability(
    function_name: str, extension: str, failure: str, expected_message: str, should_link: bool
):
    raw_fname = f"<report&>.{extension}"
    escaped_fname = f"&lt;report&amp;&gt;.{extension}"
    path = f"/workspace/{raw_fname}"
    helpers, setup = _downloadable_preview_harness(function_name)
    if failure == "network":
        fetch_impl = "const fetch=()=>Promise.reject(new TypeError('network'));"
    else:
        fetch_impl = f"const fetch=()=>Promise.resolve({{ok:false,status:{failure}}});"
    script = f"""
const el={{dataset:{{path:{json.dumps(path)}}},setAttribute(){{}},outerHTML:'',parentNode:{{}}}};
const root={{querySelectorAll(){{return [el];}}}};
const translations={{
  media_preview_unauthorized:'UNAUTHORIZED',
  media_preview_forbidden:'FORBIDDEN',
  media_preview_not_found:'NOT FOUND',
  csv_error:'FORMAT ERROR',pdf_error:'FORMAT ERROR',html_error:'FORMAT ERROR'
}};
const t=(key)=>translations[key]||key;
const esc=(value)=>String(value).replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
{fetch_impl}
{setup}
{''.join(helpers)}
{function_name}(root);
setTimeout(()=>process.stdout.write(JSON.stringify({{html:el.outerHTML}})),0);
"""
    result = _run_node(script)
    html = result["html"]
    assert expected_message in html
    assert escaped_fname in html
    assert raw_fname not in html
    if should_link:
        assert "msg-media-link" in html
        assert f'download="{escaped_fname}"' in html
    else:
        assert "msg-media-link" not in html
        assert "<a " not in html
