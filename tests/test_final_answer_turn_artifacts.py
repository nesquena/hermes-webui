import json
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _function_source(path, start, end):
    source = (ROOT / path).read_text(encoding="utf-8")
    return source[source.index(start) : source.index(end)]


def _run_node(script):
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_turn_artifact_references_require_server_landed_descriptors():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("const ARTIFACT_IGNORE_RE")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'write_file'},{path:'output/mismatch.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'read_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'read_file',arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:'output/missing-id.md',workspace_root:'/workspace',tool_call_id:'call-missing',tool_name:'write_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',is_error:true,artifacts:[{path:'output/report.md',workspace_root:'/workspace'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',output:'```diff\\n+++ output/inferred.md\\n```'}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'},{path:'output/notes.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',tid:'tid-1',artifacts:[{path:'output/tid.md',workspace_root:'/workspace',tool_call_id:'tid-1',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',preview:JSON.stringify({success:true,files_modified:['output/rejected.md']})}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:['invalid'],workspace_root:123,tool_call_id:'call-3',tool_name:'write_file'}]})"
        + "]));"
    )
    assert output == [
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
            }
        ],
        [],
        [],
        [],
        [],
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
            {
                "path": "output/notes.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
        ],
        [
            {
                "path": "output/tid.md",
                "workspace_root": "/workspace",
                "tool_call_id": "tid-1",
                "tool_name": "patch",
            }
        ],
        [],
        [],
    ]


def test_turn_artifact_references_require_strict_identity_fields():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _normalizeTypedArtifactPath(path){")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',session_id:'sid-owner',artifacts:[{path:'output/report.md',workspace_root:'/workspace',session_id:'sid-artifact',tool_call_id:'call-1',tool_name:'write_file'}]})"
        + ","
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:1,artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:1,tool_name:'write_file'}]})"
        + "]));"
    )
    assert output == [
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "session_id": "sid-artifact",
            },
        ],
        [],
    ]


def test_typed_artifact_paths_preserve_punctuation_and_supported_length():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _normalizeTypedArtifactPath(path){")
    end = workspace.index("const _turnMutatedPreviewPaths")
    boundary = "output/" + "a" * (512 - len("output/") - 1) + ";"
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',artifacts:[{path:'output/report;',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'write_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-2',artifacts:[{path:" + json.dumps(boundary) + ",workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'write_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-3',artifacts:[{path:" + json.dumps(boundary + 'x') + ",workspace_root:'/workspace',tool_call_id:'call-3',tool_name:'write_file'}]})"
        + ",turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-4',artifacts:[{path:' report.md ',workspace_root:'/workspace',tool_call_id:'call-4',tool_name:'write_file'}]})"
        + "]));"
    )
    assert output[0][0]["path"] == "output/report;"
    assert output[1][0]["path"] == boundary
    assert output[2] == []
    assert output[3][0]["path"] == " report.md "


def test_typed_artifact_path_reaches_real_route_without_rebinding():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _escapeGrantStore")
    end = workspace.index("async function authorizeWorkspaceEscapeNavigation")
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    output = _run_node(
        workspace[start:end]
        + owner_helpers
        + "\nconst S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        + "const owner={session_id:'sid-1',workspace_root:'/workspace'};\n"
        + "const spaced=_workspaceRouteForPathRel(' report.md ','read',{owner,_preserveArtifactPath:true});\n"
        + "const sibling=_workspaceRouteForPathRel('report.md','read',{owner,_preserveArtifactPath:true});\n"
        + "console.log(JSON.stringify({spaced:new URLSearchParams(spaced.split('?')[1]).get('path'),sibling:new URLSearchParams(sibling.split('?')[1]).get('path')}));"
    )
    assert output == {"spaced": " report.md ", "sibling": "report.md"}


def test_exact_routes_have_an_explicit_root_sentinel_and_fail_closed():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _escapeGrantStore")
    end = workspace.index("async function authorizeWorkspaceEscapeNavigation")
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    output = _run_node(
        workspace[start:end]
        + owner_helpers
        + "\nconst S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        + "const owner={session_id:'sid-1',workspace_root:'/workspace'};\n"
        + "const route=(path,kind)=>_workspaceRouteForPathRel(path,kind,{owner,_preserveArtifactPath:true});\n"
        + "console.log(JSON.stringify({root:route('.','list'),invalidList:route('.','read'),invalidRaw:route('.','raw'),invalidNested:route('./child','list'),malformedOwner:_workspaceRouteForPathRel('report.md','read',{owner:{workspace_root:'/workspace'}})}));"
    )
    assert output["root"].endswith("path=.")
    assert output["invalidList"] == ""
    assert output["invalidRaw"] == ""
    assert output["invalidNested"] == ""
    assert output["malformedOwner"] == ""


def test_direct_open_file_rejects_malformed_typed_paths_before_generation():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    open_file = workspace[workspace.index("async function openFile") : workspace.index("function downloadFile")]
    output = _run_node(
        "const S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        "let generations=0; let _workspaceOpenGeneration=0;\n"
        "const _nextWorkspaceOpenGeneration=()=>{generations+=1; return ++_workspaceOpenGeneration;};\n"
        "const _artifactOwnerFromOptions=(opts)=>opts.owner;\n"
        "const _artifactOwnerMatchesSession=()=>true;\n"
        + open_file
        + "Promise.all(["
        + "openFile('bad\\0.md',{owner:{session_id:'sid-1',workspace_root:'/workspace'}}),"
        + "openFile('C:/bad.md',{owner:{session_id:'sid-1',workspace_root:'/workspace'}}),"
        + "openFile('a'.repeat(513)+'.md',{owner:{session_id:'sid-1',workspace_root:'/workspace'},_preserveArtifactPath:true})"
        + "]).then(()=>console.log(JSON.stringify({generations,_workspaceOpenGeneration})));"
    )
    assert output == {"generations": 0, "_workspaceOpenGeneration": 0}


def test_public_artifact_existence_flow_uses_root_sentinel_and_rejects_invalid_exact_input():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    route_helpers = workspace[
        workspace.index("function _escapeGrantStore") : workspace.index(
            "async function authorizeWorkspaceEscapeNavigation"
        )
    ]
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    path_exists = workspace[
        workspace.index("async function _workspacePathExists") : workspace.index("function _artifactOwnerFromArtifactValue")
    ]
    artifact_owner = workspace[
        workspace.index("function _artifactOwnerFromArtifactValue") : workspace.index("async function openArtifactPath")
    ]
    open_artifact = workspace[
        workspace.index("async function openArtifactPath") : workspace.index("// ── Workspace file-tree")
    ]
    output = _run_node(
        "const requests=[]; const S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        "const api=(route)=>{requests.push(route); return Promise.resolve({entries:[{path:'report.md',name:'report.md'}]});};\n"
        "const ensureWorkspacePreviewVisible=()=>{}; const switchWorkspacePanelTab=()=>{}; const openFile=()=>Promise.resolve();\n"
        "const setStatus=()=>{}; const t=(value)=>value;\n"
        + route_helpers
        + owner_helpers
        + path_exists
        + artifact_owner
        + open_artifact
        + "async function run(){ await openArtifactPath('report.md'); await openArtifactPath({path:'./malformed.md',owner:{session_id:'sid-1',workspace_root:'/workspace'}}); await openArtifactPath({path:'dir\\\\report.md',owner:{session_id:'sid-1',workspace_root:'/workspace'}}); console.log(JSON.stringify(requests)); } run().catch((error)=>{console.error(error);process.exit(1)});"
    )
    assert len(output) == 1
    params = __import__("urllib.parse").parse.parse_qs(output[0].split("?", 1)[1])
    assert params["path"] == ["."]
    assert params["session_id"] == ["sid-1"]


def test_direct_open_file_rejects_malformed_explicit_owner_before_generation():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    open_file = workspace[workspace.index("async function openFile") : workspace.index("function downloadFile")]
    output = _run_node(
        "const S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        + owner_helpers
        + open_file
        + "Promise.all(["
        + "openFile('report.md',{owner:{workspace_root:'/workspace'}}),"
        + "openFile('report.md',{owner:null}),"
        + "openFile('report.md',{owner:{session_id:'sid-old',workspace_root:'/workspace'}})"
        + "]).then(()=>console.log(JSON.stringify({_workspaceOpenGeneration})));"
    )
    assert output == {"_workspaceOpenGeneration": 0}


def test_preview_continuations_keep_captured_owner_and_exact_path_in_routes():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    route_helpers = workspace[
        workspace.index("function _escapeGrantStore") : workspace.index(
            "async function authorizeWorkspaceEscapeNavigation"
        )
    ]
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    path_exists = workspace[
        workspace.index("async function _workspacePathExists") : workspace.index("function _artifactOwnerFromArtifactValue")
    ]
    artifact_owner = workspace[
        workspace.index("function _artifactOwnerFromArtifactValue") : workspace.index("async function openArtifactPath")
    ]
    open_artifact = workspace[
        workspace.index("async function openArtifactPath") : workspace.index("// ── Workspace file-tree")
    ]
    open_file = workspace[workspace.index("async function openFile") : workspace.index("function downloadFile")]
    refresh = workspace[
        workspace.index("function _isOpenPreviewPathMutated()") : workspace.index("function collectSessionArtifacts")
    ]
    force_render = workspace[
        workspace.index("function forceRenderMarkdownPreview()") : workspace.index("let _previewCurrentPath")
    ]
    open_browser = workspace[
        workspace.index("function openInBrowser()") : workspace.index("// openInBrowser keeps")
    ]
    output = _run_node(
        "const S={session:{session_id:'sid-1',workspace:'/workspace'}};\n"
        "const routes=[]; const nodes=new Proxy({}, {get:(target,key)=>target[key]||(target[key]={style:{display:'none'},classList:{add(){},remove(){}},textContent:'',innerHTML:'',src:'',onerror:null})});\n"
        "const $=(id)=>nodes[id]; const document={baseURI:'',createElement:()=>({}),body:{}}; const window={open:(url)=>routes.push(url)};\n"
        "const api=(route)=>{routes.push(route); const params=new URLSearchParams(route.split('?')[1]); if(route.includes('/api/list?')) return Promise.resolve({entries:[{path:' dir / report.md ',name:' report.md '},{path:'dir/report.md',name:'report.md'}]}); return Promise.resolve({content:'# spaced'});};\n"
        "const fileExt=(path)=>path.slice(path.lastIndexOf('.')).toLowerCase(); const MD_EXTS=new Set(['.md']); const IMAGE_EXTS=new Set(); const AUDIO_EXTS=new Set(); const VIDEO_EXTS=new Set(); const PDF_EXTS=new Set(); const HTML_EXTS=new Set(); const DOWNLOAD_EXTS=new Set();\n"
        "const renderFileBreadcrumb=()=>{}; const showPreview=()=>{}; const renderMarkdownPreviewContent=()=>{}; const renderCodePreviewContent=()=>{}; const shouldRenderMarkdownPreviewAsPlainText=()=>false; const setLargeMarkdownForceRenderVisible=()=>{}; const setStatus=()=>{}; const t=(value)=>value; const downloadFile=()=>{};\n"
        "const ensureWorkspacePreviewVisible=()=>{}; const switchWorkspacePanelTab=()=>{};\n"
        "let _previewCurrentPath=''; let _previewOwner=null; let _previewPreserveArtifactPath=false; let _previewRawContent=''; let _previewRawContentPath=''; let _previewDirty=false; let _previewServerEditable=null; let _previewPreviewKind=''; let _previewOfficeFormat=''; let _previewSaveRoute='';\n"
        "const _turnMutatedPreviewPaths=new Set();\n"
        "const _normalizeArtifactPath=(path)=>String(path||'').trim();\n"
        + route_helpers
        + owner_helpers
        + path_exists
        + artifact_owner
        + open_artifact
        + open_file
        + refresh
        + force_render
        + open_browser
        + "async function run(){ const owner={session_id:'sid-1',workspace_root:'/workspace'}; await openArtifactPath({path:' dir / report.md ',owner}); _turnMutatedPreviewPaths.add('dir / report.md'); await refreshOpenPreviewIfMutated(); forceRenderMarkdownPreview(); openInBrowser(); console.log(JSON.stringify(routes)); } run().catch((error)=>{console.error(error);process.exit(1)});"
    )
    assert len(output) == 4
    list_params = __import__("urllib.parse").parse.parse_qs(output[0].split("?", 1)[1])
    assert list_params["session_id"] == ["sid-1"]
    assert list_params["path"] == [" dir "]
    for route in output[1:3]:
        params = __import__("urllib.parse").parse.parse_qs(route.split("?", 1)[1])
        assert params["session_id"] == ["sid-1"]
        assert params["path"] == [" dir / report.md "]
    browser = __import__("urllib.parse").parse.parse_qs(output[3].split("?", 1)[1])
    assert browser["session_id"] == ["sid-1"]
    assert browser["path"] == [" dir / report.md "]
    assert browser["inline"] == ["1"]


def test_preview_download_preserves_captured_owner_and_exact_path():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    owner_helpers = workspace[
        workspace.index("function _artifactScalarString(value){") : workspace.index(
            "function _artifactCandidatesFromText", workspace.index("function _artifactScalarString(value){")
        )
    ]
    wrapper = workspace[
        workspace.index("function downloadPreviewFile()") : workspace.index(
            "async function copyPreviewRelativePath()"
        )
    ]
    output = _run_node(
        "let S={session:{session_id:'sid-owner',workspace:'/workspace'}};\n"
        "let _previewCurrentPath=' report.md ';\n"
        "let _previewOwner={session_id:'sid-owner',workspace_root:'/workspace'};\n"
        "let _previewPreserveArtifactPath=true;\n"
        "const calls=[]; const downloadFile=(path,opts)=>calls.push({path,opts});\n"
        + owner_helpers
        + wrapper
        + "downloadPreviewFile();\n"
        + "S={session:{session_id:'sid-other',workspace:'/other'}};\n"
        + "downloadPreviewFile();\n"
        + "console.log(JSON.stringify(calls));"
    )
    assert output == [{
        "path": " report.md ",
        "opts": {
            "owner": {"session_id": "sid-owner", "workspace_root": "/workspace"},
            "_preserveArtifactPath": True,
        },
    }]


def test_artifact_owner_match_requires_root_when_captured():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _artifactScalarString(value){")
    end = workspace.index("function _artifactCandidatesFromText", start)
    output = _run_node(
        workspace[start:end]
        + "\nconst scenario = ["
        + "{ name: 'empty-active-root', activeRoot:'', captured:'/old' },"
        + "{ name: 'matching-root', activeRoot:'/old', captured:'/old' },"
        + "{ name: 'captured-empty', activeRoot:'/old', captured:'' },"
        + "{ name: 'missing-active-root-with-captured', activeRoot:'', captured:'/old' },"
        + "];\n"
        + "console.log(JSON.stringify(scenario.map((entry) => {\n"
        + "  global.S = { session: { session_id:'sid-1', workspace: entry.activeRoot } };\n"
        + "  return _artifactOwnerMatchesSession({\n"
        + "    session_id:'sid-1',\n"
        + "    workspace_root: entry.captured,\n"
        + "  });\n"
        + "})));"
    )
    assert output == [False, True, True, False]


def test_current_session_artifact_owner_requires_non_empty_string_id_and_trims_once():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("function _artifactScalarString(value){")
    end = workspace.index("function _artifactCandidatesFromText", start)
    output = _run_node(
        workspace[start:end]
        + "\nconst cases = ["
        + "{session_id:'  sid-current  ',workspace:'/workspace'},"
        + "{session_id:'   ',workspace:'/workspace'},"
        + "{session_id:'',workspace:'/workspace'},"
        + "{session_id:42,workspace:'/workspace'},"
        + "{session_id:{value:'sid-object'},workspace:'/workspace'},"
        + "null"
        + "];\n"
        + "console.log(JSON.stringify(cases.map((session)=>{ global.S={session}; return _artifactOwnerFromCurrentSession(); })));"
    )
    assert output == [
        {"session_id": "sid-current", "workspace_root": "/workspace"},
        None,
        None,
        None,
        None,
        None,
    ]


def test_native_preview_owner_switch_fences_old_loads_and_clears_all_sinks():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    helpers = workspace[workspace.index("const ARTIFACT_IGNORE_RE") : workspace.index("async function _workspacePathExists")]
    open_file = workspace[
        workspace.index("async function openFile") : workspace.index("function downloadFile")
    ]
    clear_preview = _function_source(
        "static/boot.js", "function clearPreview", "function _applySessionContextMetadataUpdate"
    )
    output = _run_node(
        "const status=[]; const dom=[];\n"
        "class Node{constructor(id){this.id=id;this.style={display:''};this.classList={add(){},remove(){}};this.src='';this.onerror=null;this.onload=null;this.innerHTML='';this.children=[];this.paused=false;this.title='';this.alt='';}\n"
        "set innerHTML(value){this._innerHTML=String(value);this.children=[];if(this._innerHTML){const child=new Node('media');child.src=(this._innerHTML.match(/src=\\\"([^\\\"]*)/)||[])[1]||'';this.children.push(child);}}\n"
        "get innerHTML(){return this._innerHTML||'';} pause(){this.paused=true;dom.push(this.id+':pause');} load(){dom.push(this.id+':load');} querySelectorAll(){return this.children;} setAttribute(){} appendChild(){} }\n"
        "const nodes={}; const $=(id)=>nodes[id]||(nodes[id]=new Node(id));\n"
        "const document={createElement:()=>new Node('created'),body:{appendChild(){},removeChild(){}}}; const window={};\n"
        "const S={session:{session_id:'sid-a',workspace:'/workspace-a'}};\n"
        "const IMAGE_EXTS=new Set(['.png']); const AUDIO_EXTS=new Set(['.mp3']); const VIDEO_EXTS=new Set(['.mp4']); const PDF_EXTS=new Set(['.pdf']); const MD_EXTS=new Set(['.md']); const HTML_EXTS=new Set(['.html']); const DOWNLOAD_EXTS=new Set();\n"
        "const fileExt=(path)=>path.slice(path.lastIndexOf('.')).toLowerCase(); const t=(value)=>value; const setStatus=(value)=>status.push(value);\n"
        "const showPreview=(mode)=>{ $('previewArea').classList.add('visible'); $('previewBadge').textContent=mode; }; const renderFileBreadcrumb=()=>{}; const renderMarkdownPreviewContent=()=>{}; const renderCodePreviewContent=()=>{}; const renderCsvPreviewContent=()=>false; const shouldRenderMarkdownPreviewAsPlainText=()=>false; const setLargeMarkdownForceRenderVisible=()=>{}; const largeMarkdownPlainTextStatus=()=>'';\n"
        "const _workspaceRouteForPath=()=>'/raw'; const _workspaceRouteForPathRel=(path,kind,opts={})=>typeof _artifactOwnerFromOptions==='function'&&_artifactOwnerFromOptions(opts)?'/raw':''; const _workspaceEscapeGrantForPath=()=>null; const _clearWorkspaceEscapeGrant=()=>{}; const _mediaPlayerHtml=(mode,url)=>`<${mode} src=\\\"${url}\\\" controls></${mode}>`; const _applyMediaPlaybackPreferences=()=>{}; const showToast=()=>{};\n"
        "const api=()=>Promise.resolve({content:'# loaded'}); let _workspacePanelMode='preview'; const closeWorkspacePanel=()=>{}; const openWorkspacePanel=()=>{}; const syncWorkspacePanelUI=()=>{}; const renderBreadcrumb=()=>{};\n"
        "const handleWorkspaceClose=()=>{};\n"
        "let _previewCurrentPath=''; let _previewOwner=null; let _previewPreserveArtifactPath=false; let _previewCurrentMode=''; let _previewDirty=false; let _previewServerEditable=null; let _previewSaveRoute=''; let _previewOfficeFormat=''; let _previewPreviewKind=''; let _previewRawContent=''; let _previewRawContentPath='';\n"
        + helpers
        + open_file
        + clear_preview
        + "_installWorkspaceSessionOwnerFence();\n"
        + "async function run(){\n"
        + "  const ownerA={session_id:'sid-a',workspace_root:'/workspace-a'}; const ownerB={session_id:'sid-b',workspace_root:'/workspace-b'};\n"
        + "  await openFile('output/a.png',{owner:ownerA}); const staleImageError=$('previewImg').onerror;\n"
        + "  S.session={session_id:'sid-b',workspace:'/workspace-b'}; await openFile('output/b.png',{owner:ownerB}); const bImage={path:_previewCurrentPath,src:$('previewImg').src,status:status.length}; staleImageError(); const imageControl={path:_previewCurrentPath,src:$('previewImg').src,status:status.length,bOwner:_previewOwner&&_previewOwner.session_id};\n"
        + "  S.session={session_id:'sid-a',workspace:'/workspace-a'}; await openFile('output/a.html',{owner:ownerA}); const html=$('previewHtmlIframe'); const staleHtmlSrc=html.src; html.onload=()=>{status.push('stale-html-load');html.src=staleHtmlSrc+'#settled';}; html.onerror=()=>status.push('stale-html-error');\n"
        + "  S.session={session_id:'sid-b',workspace:'/workspace-b'}; const switchedIframe={src:html.src,status:status.length,onload:html.onload,onerror:html.onerror}; if(typeof html.onload==='function')html.onload(); if(typeof html.onerror==='function')html.onerror(); await openFile('output/b.html',{owner:ownerB}); const bHtmlSrc=html.src; const iframeControl={path:_previewCurrentPath,src:html.src,expected:bHtmlSrc,status:status.length,switched:switchedIframe};\n"
        + "  S.session={session_id:'sid-a',workspace:'/workspace-a'}; await openFile('output/a.mp3',{owner:ownerA}); const media=$('previewMediaWrap'); const staleMedia=media.children[0]; if(staleMedia)staleMedia.onerror=()=>status.push('stale-media-error'); const staleMediaHtml=media.innerHTML;\n"
        + "  S.session={session_id:'sid-b',workspace:'/workspace-b'}; clearPreview(); const mediaCleared={html:media.innerHTML,children:media.children.length,paused:!!(staleMedia&&staleMedia.paused),dom:dom.slice(),status:status.length};\n"
        + "  await openFile('output/b.png',{owner:ownerB}); const bError=$('previewImg').onerror; bError(); const currentOwnerControl={path:_previewCurrentPath,owner:_previewOwner&&_previewOwner.session_id,status:status.slice()};\n"
        + "  S.session={session_id:42,workspace:'/workspace-b'}; const numericBefore={generation:_workspaceOpenGeneration,path:_previewCurrentPath,src:$('previewImg').src,status:status.length}; const numericRoute=_workspaceRouteForPathRel('output/numeric.png','raw'); await openFile('output/numeric.png'); const numericAfter={generation:_workspaceOpenGeneration,path:_previewCurrentPath,src:$('previewImg').src,status:status.length,owner:_artifactOwnerFromCurrentSession()};\n"
        + "  S.session={session_id:{value:'sid-object'},workspace:'/workspace-b'}; const objectBefore={generation:_workspaceOpenGeneration,path:_previewCurrentPath,src:$('previewImg').src,status:status.length}; const objectRoute=_workspaceRouteForPathRel('output/object.png','raw'); await openFile('output/object.png'); const objectAfter={generation:_workspaceOpenGeneration,path:_previewCurrentPath,src:$('previewImg').src,status:status.length,owner:_artifactOwnerFromCurrentSession()};\n"
        + "  console.log(JSON.stringify({bImage, imageControl, iframeControl, mediaCleared, currentOwnerControl, numeric:{before:numericBefore,after:numericAfter,route:numericRoute}, object:{before:objectBefore,after:objectAfter,route:objectRoute}}));\n"
        + "} run().catch(error=>{console.error(error);process.exit(1)});"
    )
    assert output["bImage"] == {"path": "output/b.png", "src": "/raw", "status": 0}
    assert output["imageControl"] == {
        "path": "output/b.png",
        "src": "/raw",
        "status": 0,
        "bOwner": "sid-b",
    }
    assert output["iframeControl"]["path"] == "output/b.html"
    assert output["iframeControl"]["src"] == output["iframeControl"]["expected"] == "/raw"
    assert output["iframeControl"]["status"] == 0
    assert output["iframeControl"]["switched"] == {
        "src": "",
        "status": 0,
        "onload": None,
        "onerror": None,
    }
    assert output["mediaCleared"]["html"] == ""
    assert output["mediaCleared"]["children"] == 0
    assert output["mediaCleared"]["paused"] is True
    assert output["mediaCleared"]["status"] == 0
    assert output["currentOwnerControl"] == {
        "path": "output/b.png",
        "owner": "sid-b",
        "status": ["image_load_failed"],
    }
    assert output["numeric"]["route"] == ""
    assert output["numeric"]["after"] == {
        **output["numeric"]["before"],
        "owner": None,
    }
    assert output["object"]["route"] == ""
    assert output["object"]["after"] == {
        **output["object"]["before"],
        "owner": None,
    }


def test_active_workspace_owner_transition_is_the_only_session_workspace_publisher():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    panels = (ROOT / "static/panels.js").read_text(encoding="utf-8")
    all_static_js = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "static").glob("*.js"))
    )

    assert "function _transitionActiveSessionWorkspaceOwner(" in workspace
    assert re.search(r"S\.session\.workspace\s*=(?!=)", all_static_js) is None

    switch_start = panels.index("async function switchToWorkspace(path,name){")
    switch_end = panels.index("// ── Profile panel + dropdown", switch_start)
    switch_source = panels[switch_start:switch_end]
    assert "_transitionActiveSessionWorkspaceOwner(" in switch_source

    profile_start = panels.index("async function switchToProfile(name) {")
    profile_end = panels.index("function openProfileCreate(){", profile_start)
    profile_source = panels[profile_start:profile_end]
    assert "_transitionActiveSessionWorkspaceOwner(" in profile_source

    load_start = workspace.index("async function loadDir(path, opts={}){")
    load_end = workspace.index("function refreshWorkspacePanel(){", load_start)
    load_source = workspace[load_start:load_end]
    assert "_transitionActiveSessionWorkspaceOwner(" in load_source


def test_production_composed_same_session_workspace_switch_clears_native_sinks_before_refresh():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    helpers = workspace[workspace.index("const ARTIFACT_IGNORE_RE") : workspace.index("async function _workspacePathExists")]
    open_file = workspace[
        workspace.index("async function openFile") : workspace.index("function downloadFile")
    ]
    panels = (ROOT / "static/panels.js").read_text(encoding="utf-8")
    switch_to_workspace = panels[
        panels.index("async function switchToWorkspace(path,name){") : panels.index(
            "// ── Profile panel + dropdown", panels.index("async function switchToWorkspace(path,name){")
        )
    ]
    output = _run_node(
        "const events=[]; const status=[]; let refreshGate;\n"
        "const refreshWait = new Promise(resolve=>{ refreshGate=resolve; });\n"
        "class Node{constructor(id){this.id=id;this.style={display:''};this.classList={add(){},remove(){},contains(){return false;}};this.src='';this.onerror=null;this.onload=null;this.onloadeddata=null;this.oncanplay=null;this.onabort=null;this.onstalled=null;this.innerHTML='';this.children=[];this.paused=false;this.title='';this.alt='';}\n"
        "set innerHTML(value){this._innerHTML=String(value);if(!this._innerHTML)this.children=[];} get innerHTML(){return this._innerHTML||'';}\n"
        "pause(){this.paused=true;} load(){} querySelectorAll(){return this.children;} removeAttribute(name){if(name==='src')this.src='';} setAttribute(){} appendChild(node){this.children.push(node);} }\n"
        "const nodes={}; const $=(id)=>nodes[id]||(nodes[id]=new Node(id));\n"
        "const document={createElement:()=>new Node('created'),body:{appendChild(){},removeChild(){}}}; const window={_newChatOnWorkspaceSwitch:false};\n"
        "const S={session:{session_id:'sid-same',workspace:'/workspace-a'},messages:[],busy:false,currentDir:'.',_profileSwitchWorkspace:null,_pendingSessionToolsets:null};\n"
        "const IMAGE_EXTS=new Set(['.png']); const AUDIO_EXTS=new Set(['.mp3']); const VIDEO_EXTS=new Set(['.mp4']); const PDF_EXTS=new Set(['.pdf']); const MD_EXTS=new Set(['.md']); const HTML_EXTS=new Set(['.html']); const DOWNLOAD_EXTS=new Set();\n"
        "const fileExt=(path)=>path.slice(path.lastIndexOf('.')).toLowerCase(); const t=(value)=>value; const setStatus=(value)=>status.push(value); const showToast=()=>{};\n"
        "const showPreview=(mode)=>{ $('previewArea').classList.add('visible'); $('previewBadge').textContent=mode; }; const renderFileBreadcrumb=()=>{}; const renderMarkdownPreviewContent=()=>{}; const renderCodePreviewContent=()=>{}; const renderCsvPreviewContent=()=>false; const shouldRenderMarkdownPreviewAsPlainText=()=>false; const setLargeMarkdownForceRenderVisible=()=>{}; const largeMarkdownPlainTextStatus=()=>'';\n"
        "const _workspaceRouteForPath=()=>'/raw'; const _workspaceRouteForPathRel=()=>'/raw'; const _workspaceEscapeGrantForPath=()=>null; const _clearWorkspaceEscapeGrant=()=>{}; const _mediaPlayerHtml=(mode,url)=>`<${mode} src=\"${url}\" controls></${mode}>`; const _applyMediaPlaybackPreferences=()=>{};\n"
        "const api=(route)=>{ if(route==='/api/session/update'){ events.push({event:'api-update',generation:_workspaceOpenGeneration,src:$('previewImg').src}); return Promise.resolve({}); } return Promise.resolve({content:'# loaded'}); };\n"
        "const bumpWorkspaceTreeGen=()=>{}; const closeWsDropdown=()=>{}; const syncTopbar=()=>{}; const getWorkspaceFriendlyName=(path)=>path; const _currentPanel='chat';\n"
        "const _previewDirty=false; let _previewCurrentPath=''; let _previewOwner=null; let _previewPreserveArtifactPath=false; let _previewCurrentMode=''; let _previewServerEditable=null; let _previewSaveRoute=''; let _previewOfficeFormat=''; let _previewPreviewKind=''; let _previewRawContent=''; let _previewRawContentPath='';\n"
        "const loadDir=async()=>{ events.push({event:'refresh-start',generation:_workspaceOpenGeneration,key:_workspaceSessionOwnerKey,owner:S.session&&{session_id:S.session.session_id,workspace:S.session.workspace},imageSrc:$('previewImg').src,pdfSrc:$('previewPdfFrame').src,htmlSrc:$('previewHtmlIframe').src,mediaHtml:$('previewMediaWrap').innerHTML,mediaChildren:$('previewMediaWrap').children.length}); await refreshWait; events.push({event:'refresh-done'}); };\n"
        + helpers
        + open_file
        + switch_to_workspace
        + "_installWorkspaceSessionOwnerFence();\n"
        + "const clearNativePreviewSinks=_clearNativePreviewSinks; _clearNativePreviewSinks=(opts)=>{ events.push({event:'clear',bump:!opts||opts.bumpGeneration!==false,key:_workspaceSessionOwnerKey,owner:S.session&&{session_id:S.session.session_id,workspace:S.session.workspace},imageSrc:$('previewImg').src}); return clearNativePreviewSinks(opts); };\n"
        + "async function run(){\n"
        + "  const ownerA={session_id:'sid-same',workspace_root:'/workspace-a'}; const ownerB={session_id:'sid-same',workspace_root:'/workspace-b'};\n"
        + "  await openFile('output/a.png',{owner:ownerA}); const staleImageError=$('previewImg').onerror;\n"
        + "  const pdf=$('previewPdfFrame'); pdf.src='/raw/a.pdf'; pdf.onload=()=>status.push('stale-pdf-load');\n"
        + "  const html=$('previewHtmlIframe'); html.src='/raw/a.html'; html.onload=()=>status.push('stale-html-load'); html.onerror=()=>status.push('stale-html-error');\n"
        + "  const media=$('previewMediaWrap'); const audio=new Node('audio-a'); const video=new Node('video-a'); audio.src='/raw/a.mp3'; video.src='/raw/a.mp4'; audio.onloadeddata=()=>status.push('stale-audio'); video.oncanplay=()=>status.push('stale-video'); media.children=[audio,video]; media.innerHTML='<audio src=\"/raw/a.mp3\"></audio><video src=\"/raw/a.mp4\"></video>';\n"
        + "  const before={generation:_workspaceOpenGeneration,imageSrc:$('previewImg').src,pdfSrc:pdf.src,htmlSrc:html.src,mediaChildren:media.children.length};\n"
        + "  const switchTask=switchToWorkspace('/workspace-b','B'); await Promise.resolve();\n"
        + "  const refresh=events.find(entry=>entry.event==='refresh-start'); const transitionClear=events.find(entry=>entry.event==='clear'&&entry.bump); const afterBeforeAwait={before,refresh,transitionClear,generation:_workspaceOpenGeneration,owner:{session_id:S.session.session_id,workspace:S.session.workspace},imageHandler:$('previewImg').onerror,pdfHandler:pdf.onload,htmlHandler:html.onload,mediaHtml:media.innerHTML,mediaChildren:media.children.length,audio:{src:audio.src,paused:audio.paused,onloadeddata:audio.onloadeddata},video:{src:video.src,paused:video.paused,oncanplay:video.oncanplay}};\n"
        + "  staleImageError(); if(typeof pdf.onload==='function')pdf.onload(); if(typeof html.onload==='function')html.onload(); if(typeof html.onerror==='function')html.onerror(); if(typeof audio.onloadeddata==='function')audio.onloadeddata(); if(typeof video.oncanplay==='function')video.oncanplay();\n"
        + "  refreshGate(); await switchTask; await openFile('output/b.pdf',{owner:ownerB}); const bAuthoritative={generation:_workspaceOpenGeneration,owner:_previewOwner,src:$('previewPdfFrame').src,path:_previewCurrentPath,status:status.slice()};\n"
        + "  const sameGeneration=_workspaceOpenGeneration; const sameTask=switchToWorkspace('/workspace-b','B'); await sameTask; const sameOwner={generation:_workspaceOpenGeneration,unchanged:_workspaceOpenGeneration===sameGeneration,owner:{session_id:S.session.session_id,workspace:S.session.workspace}};\n"
        + "  console.log(JSON.stringify({afterBeforeAwait,bAuthoritative,sameOwner,status}));\n"
        + "} run().catch(error=>{console.error(error);process.exit(1)});"
    )
    assert output["afterBeforeAwait"]["refresh"]["generation"] > output["afterBeforeAwait"]["before"]["generation"]
    assert output["afterBeforeAwait"]["transitionClear"]["key"] == "sid-same\u0000/workspace-a"
    assert output["afterBeforeAwait"]["transitionClear"]["owner"] == {
        "session_id": "sid-same",
        "workspace": "/workspace-a",
    }
    assert output["afterBeforeAwait"]["refresh"]["key"] == "sid-same\u0000/workspace-b"
    assert output["afterBeforeAwait"]["refresh"]["imageSrc"] == ""
    assert output["afterBeforeAwait"]["refresh"]["pdfSrc"] == ""
    assert output["afterBeforeAwait"]["refresh"]["htmlSrc"] == ""
    assert output["afterBeforeAwait"]["refresh"]["mediaHtml"] == ""
    assert output["afterBeforeAwait"]["refresh"]["mediaChildren"] == 0
    assert output["afterBeforeAwait"]["owner"] == {"session_id": "sid-same", "workspace": "/workspace-b"}
    assert output["afterBeforeAwait"]["imageHandler"] is None
    assert output["afterBeforeAwait"]["pdfHandler"] is None
    assert output["afterBeforeAwait"]["htmlHandler"] is None
    assert output["afterBeforeAwait"]["audio"] == {"src": "", "paused": True, "onloadeddata": None}
    assert output["afterBeforeAwait"]["video"] == {"src": "", "paused": True, "oncanplay": None}
    assert output["status"] == []
    assert output["bAuthoritative"]["owner"] == {
        "session_id": "sid-same",
        "workspace_root": "/workspace-b",
    }
    assert output["bAuthoritative"]["path"] == "output/b.pdf"
    assert output["bAuthoritative"]["src"] == "/raw"
    assert output["sameOwner"]["unchanged"] is True


def test_artifact_open_aborts_stale_owner_async_sinks_and_image_error():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    helpers = workspace[workspace.index("const ARTIFACT_IGNORE_RE") : workspace.index("async function _workspacePathExists")]
    exists = workspace[workspace.index("async function _workspacePathExists") : workspace.index("// ── Workspace file-tree")]
    open_file = workspace[workspace.index("async function openFile") : workspace.index("function downloadFile")]
    output = _run_node(
        "const pending = [];\n"
        "const status = [];\n"
        "let previewMutations = 0;\n"
        "let openMutations = 0;\n"
        "let downloadMutations = 0;\n"
        "let breadcrumbMutations = 0;\n"
        "let domMutations = 0;\n"
        "const nodes = new Proxy({}, {get: (_target, key) => {\n"
        "  if(!(key in _target)){ const node={textContent:'',style:{},classList:{add(){},remove(){}},\n"
        "    appendChild(){},setAttribute(){},innerHTML:'',src:'',onerror:null};\n"
        "    _target[key] = new Proxy(node,{set(target,name,value){domMutations++; target[name]=value; return true;}}); }\n"
        "  return _target[key];\n"
        "}});\n"
        "const $ = (id) => nodes[id];\n"
        "const document = {createElement: () => ({style:{},classList:{add(){},remove(){}},appendChild(){},click(){},setAttribute(){}}), body:{appendChild(){},removeChild(){}}};\n"
        "const window = {};\n"
        "const S = {session:{session_id:'sid-1',workspace:'/old'}};\n"
        "const IMAGE_EXTS = new Set(['.png']); const AUDIO_EXTS = new Set(); const VIDEO_EXTS = new Set();\n"
        "const PDF_EXTS = new Set(); const MD_EXTS = new Set(['.md']); const HTML_EXTS = new Set(); const DOWNLOAD_EXTS = new Set();\n"
        "const api = () => new Promise((resolve, reject) => pending.push({resolve, reject}));\n"
        "const ensureWorkspacePreviewVisible = () => { openMutations++; };\n"
        "const switchWorkspacePanelTab = () => { openMutations++; };\n"
        "const setStatus = (value) => status.push(value); const t = (value) => value;\n"
        "const fileExt = (path) => path.slice(path.lastIndexOf('.')).toLowerCase();\n"
        "const showPreview = () => { previewMutations++; }; const renderFileBreadcrumb = () => { breadcrumbMutations++; };\n"
        "const renderMarkdownPreviewContent = () => { previewMutations++; };\n"
        "const renderCodePreviewContent = () => { previewMutations++; };\n"
        "const downloadFile = () => { downloadMutations++; };\n"
        "const _workspaceRouteForPath = () => '/raw'; const _workspaceRouteForPathRel = () => '/list';\n"
        "const _workspaceEscapeGrantForPath = () => null; const _clearWorkspaceEscapeGrant = () => {};\n"
        "const showToast = () => {}; const _mediaPlayerHtml = () => '';\n"
        "const shouldRenderMarkdownPreviewAsPlainText = () => false; const setLargeMarkdownForceRenderVisible = () => {};\n"
        "const largeMarkdownPlainTextStatus = () => ''; let _previewServerEditable = null;\n"
        "let _previewSaveRoute = ''; let _previewOfficeFormat = ''; let _previewPreviewKind = '';\n"
        "let _previewCurrentPath = ''; let _previewRawContent = ''; let _previewRawContentPath = '';\n"
        "const _turnArtifactEntriesFromScene = () => [];\n"
        + helpers
        + exists
        + open_file
        + "async function settleStaleRead(settle){\n"
        + "  const task = openArtifactPath('output/report.md');\n"
        + "  pending.shift().resolve({entries:[{path:'output/report.md'}]}); await new Promise((resolve)=>setTimeout(resolve,0));\n"
        + "  const before = {status:status.length,preview:previewMutations,open:openMutations,download:downloadMutations,breadcrumb:breadcrumbMutations,dom:domMutations,raw:_previewRawContent,rawPath:_previewRawContentPath,currentPath:_previewCurrentPath};\n"
        + "  S.session = {session_id:'sid-1',workspace:''};\n"
        + "  settle(pending.shift()); await task;\n"
        + "  return {status:status.length-before.status,preview:previewMutations-before.preview,open:openMutations-before.open,download:downloadMutations-before.download,breadcrumb:breadcrumbMutations-before.breadcrumb,dom:domMutations-before.dom,rawUnchanged:_previewRawContent===before.raw,rawPathUnchanged:_previewRawContentPath===before.rawPath,currentPathUnchanged:_previewCurrentPath===before.currentPath};\n"
        + "}\n"
        + "async function run(){\n"
        + "  const staleResolved = await settleStaleRead((read)=>read.resolve({content:'# stale'}));\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const staleRejected = await settleStaleRead((read)=>read.reject(new Error('switched')));\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const beforeDownload = downloadMutations; const staleDownload = openArtifactPath('output/archive.txt');\n"
        + "  pending.shift().resolve({entries:[{path:'output/archive.txt'}]}); await new Promise((resolve)=>setTimeout(resolve,0));\n"
        + "  S.session = {session_id:'sid-1',workspace:''}; pending.shift().resolve({binary:true}); await staleDownload;\n"
        + "  const staleDownloadDelta = downloadMutations-beforeDownload;\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const beforePositive = {preview:previewMutations,open:openMutations,breadcrumb:breadcrumbMutations};\n"
        + "  const positive = openArtifactPath('output/report.md');\n"
        + "  pending.shift().resolve({entries:[{path:'output/report.md'}]}); await new Promise((resolve)=>setTimeout(resolve,0));\n"
        + "  pending.shift().resolve({content:'# matching'}); await positive;\n"
        + "  const positiveSummary = {preview:previewMutations-beforePositive.preview,open:openMutations-beforePositive.open,breadcrumb:breadcrumbMutations-beforePositive.breadcrumb};\n"
        + "  const absoluteTask = openArtifactPath('/old/output/legacy.md');\n"
        + "  pending.shift().resolve({entries:[{path:'output/legacy.md'}]}); await new Promise((resolve)=>setTimeout(resolve,0));\n"
        + "  pending.shift().resolve({content:'# legacy'}); await absoluteTask;\n"
        + "  const absolutePath = _previewCurrentPath;\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const currentTask = openFile('output/current.md',{owner:{session_id:'sid-1',workspace_root:'/old'}});\n"
        + "  const currentRead = pending.shift();\n"
        + "  const rejectedGeneration = {};\n"
        + "  const rejectedDirect = async (name, action) => { const before = _workspaceOpenGeneration; await action(); rejectedGeneration[name] = {before,after:_workspaceOpenGeneration}; };\n"
        + "  const rejectedStale = rejectedDirect('staleOwner',()=>openFile('output/stale.md',{owner:{session_id:'sid-old',workspace_root:'/old'}}));\n"
        + "  const rejectedMalformed = rejectedDirect('malformedPath',()=>openFile('./malformed.md',{owner:{session_id:'sid-1',workspace_root:'/old'}}));\n"
        + "  const rejectedNul = rejectedDirect('nulPath',()=>openFile('bad\\0.md',{owner:{session_id:'sid-1',workspace_root:'/old'}}));\n"
        + "  const rejectedDrive = rejectedDirect('drivePath',()=>openFile('C:/bad.md',{owner:{session_id:'sid-1',workspace_root:'/old'}}));\n"
        + "  const rejectedOverLimit = rejectedDirect('overLimitPath',()=>openFile('a'.repeat(513)+'.md',{owner:{session_id:'sid-1',workspace_root:'/old'},_preserveArtifactPath:true}));\n"
        + "  const rejectedMalformedOwner = rejectedDirect('malformedOwner',()=>openFile('output/malformed-owner.md',{owner:{workspace_root:'/old'}}));\n"
        + "  await Promise.all([rejectedStale,rejectedMalformed,rejectedNul,rejectedDrive,rejectedOverLimit,rejectedMalformedOwner]); currentRead.resolve({content:'# current'}); await currentTask;\n"
        + "  const staleOwnerPreservesCurrent = {raw:_previewRawContent,rawPath:_previewRawContentPath,currentPath:_previewCurrentPath};\n"
        + "  const ordinaryLongPath = 'nested/'.repeat(100)+'report.md';\n"
        + "  const ordinaryLongTask = openFile(ordinaryLongPath,{owner:{session_id:'sid-1',workspace_root:'/old'}});\n"
        + "  pending.shift().resolve({content:'# long'}); await ordinaryLongTask;\n"
        + "  const ordinaryLongOpenedPath = _previewCurrentPath;\n"
        + "  S.session = {session_id:'sid-1',workspace:'/old'};\n"
        + "  const olderAttempt = openFile('output/older.md',{owner:{session_id:'sid-1',workspace_root:'/old'}});\n"
        + "  const olderRead = pending.shift();\n"
        + "  const newerAttempt = openFile('output/newer.md',{owner:{session_id:'sid-1',workspace_root:'/old'}});\n"
        + "  const newerRead = pending.shift();\n"
        + "  newerRead.resolve({content:'# newer'}); olderRead.resolve({content:'# older'});\n"
        + "  await Promise.all([olderAttempt,newerAttempt]);\n"
        + "  const sameOwnerRace = {raw:_previewRawContent,rawPath:_previewRawContentPath,currentPath:_previewCurrentPath};\n"
        + "  const image = nodes.previewImg; const imageTask = openArtifactPath('output/image.png');\n"
        + "  pending.shift().resolve({entries:[{path:'output/image.png'}]}); await imageTask;\n"
        + "  S.session = {session_id:'sid-1',workspace:''}; image.onerror();\n"
        + "  console.log(JSON.stringify({staleResolved,staleRejected,staleDownload:staleDownloadDelta,positive:positiveSummary,absolutePath,ordinaryLongOpenedPath,staleOwnerPreservesCurrent,sameOwnerRace,rejectedGeneration,downloadMutations,status, imageErrorInstalled:typeof image.onerror==='function'}));\n"
        + "}\nrun().catch((error)=>{console.error(error);process.exit(1)});"
    )
    assert output["staleResolved"] == output["staleRejected"] == {
        "status": 0,
        "preview": 0,
        "open": 0,
        "download": 0,
        "breadcrumb": 0,
        "dom": 0,
        "rawUnchanged": True,
        "rawPathUnchanged": True,
        "currentPathUnchanged": True,
    }
    assert output["staleDownload"] == 0
    assert all(
        row["before"] == row["after"]
        for row in output["rejectedGeneration"].values()
    )
    assert output["positive"] == {"preview": 1, "open": 2, "breadcrumb": 1}
    assert output["absolutePath"] == "output/legacy.md"
    assert len(output["ordinaryLongOpenedPath"]) > 512
    assert output["staleOwnerPreservesCurrent"] == {"raw": "# current", "rawPath": "output/current.md", "currentPath": "output/current.md"}
    assert output["sameOwnerRace"] == {"raw": "# newer", "rawPath": "output/newer.md", "currentPath": "output/newer.md"}
    assert output["downloadMutations"] == 0
    assert output["status"] == []
    assert output["imageErrorInstalled"] is True


def test_anchor_projector_normalizes_real_artifact_event_for_renderer():
    ui_helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor"
    )
    output = _run_node(
        "const fs=require('fs'),vm=require('vm');\n"
        + "const sandbox={window:{}}; vm.createContext(sandbox); vm.runInContext(fs.readFileSync('static/assistant_turn_anchors.js','utf8'),sandbox);\n"
        + "const api=sandbox.window.HermesAssistantTurnAnchors;\n"
        + "const registry=api.createAssistantTurnAnchorRegistry({session_id:'sid-replay',turn_id:'turn-1'});\n"
        + "api.applyAssistantTurnAnchorSourceEvent(registry,{event:'artifact_reference',source_event_type:'artifact_reference',session_id:'sid-replay',payload:{path:'output/report.md',workspace_root:'/workspace',tool_name:'patch',tool_call_id:'call-replay'},event_id:'run-1:3',seq:3},{session_id:'sid-replay',stream_id:'stream-1'});\n"
        + "api.applyAssistantTurnAnchorSourceEvent(registry,{event:'artifact_reference',source_event_type:'artifact_reference',session_id:'sid-replay',payload:{path:' report.md ',workspace_root:'/workspace',tool_name:'patch',tool_call_id:'call-spaced'},event_id:'run-1:4',seq:4},{session_id:'sid-replay',stream_id:'stream-1'});\n"
        + "const scene=api.projectAssistantTurnAnchorActivityScene(registry,{mode:'compact_worklog'});\n"
        + "const S={session:{workspace:'/workspace',session_id:'sid-replay'}}; const clicked=[]; const openArtifactPath=(entry)=>clicked.push(entry);\n"
        + "const document={createElement:()=>({className:'',title:'',type:'',innerHTML:'',children:[],append(...x){this.children.push(...x)},appendChild(x){this.children.push(x)},replaceChildren(...x){this.children=[...x]},setAttribute(){},addEventListener(_name,fn){this.onclick=fn}})};\n"
        + ui_helpers
        + "const segment={children:[],querySelectorAll:()=>[],appendChild(node){this.children.push(node)}}; const message={_anchor_activity_scene:scene};\n"
        + "_renderTurnArtifactListForMessage(message,segment,0); segment.children[0].children[0].children[0].children[0].onclick(); segment.children[0].children[0].children[1].children[0].onclick();\n"
        + "console.log(JSON.stringify({scene,entries:_turnArtifactEntriesFromScene(scene),clicked}));"
    )
    artifact = output["entries"][0]
    spaced = output["entries"][1]
    assert output["scene"]["artifacts"][0]["source_event_type"] == "artifact_reference"
    assert artifact["type"] == "artifact_reference"
    assert artifact["session_id"] == "sid-replay"
    assert artifact["path"] == "output/report.md"
    assert spaced["path"] == " report.md "
    assert output["clicked"] == [artifact, spaced]


def test_replay_restore_ignores_scalar_tool_calls_and_artifacts():
    from api import routes

    descriptor = {
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "tool_call_id": "call-replay",
        "tool_name": "patch",
        "session_id": "sid-replay",
    }
    for malformed_tool_calls in (None, 1, {"id": "call-replay"}):
        messages = [
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": "final answer",
                "tool_calls": malformed_tool_calls,
                "_anchor_activity_scene": {
                    "version": "activity_scene_v1",
                    "activity_rows": [],
                    "artifacts": 1,
                },
            },
        ]
        assert routes._final_turn_artifact_paths(
            messages, workspace_root="/workspace", session_id="sid-replay"
        ) == {}
        hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
            messages, {1: [descriptor]}
        )
        assert hydrated[1]["_anchor_activity_scene"]["artifacts"] == [
            {
                "type": "artifact_reference",
                "payload": {**descriptor, "source": "transcript_replay"},
            }
        ]

    sanitized = routes._sanitize_anchor_activity_scene({
        "version": "activity_scene_v1",
        "activity_rows": [],
        "artifacts": [None, 1, {"type": "artifact_reference"}],
    })
    assert sanitized["artifacts"] == [{"type": "artifact_reference"}]


def test_turn_artifact_renderer_collapses_large_lists_with_accessible_toggle():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor"
    )
    artifacts = [
        {
            "type": "artifact_reference",
            "payload": {
                "path": f"output/report-{index}.md",
                "workspace_root": "/workspace",
                "session_id": "sid-owner",
                "tool_name": "patch",
                "tool_call_id": f"call-{index}",
            },
        }
        for index in range(12)
    ]
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        "class Node{constructor(){this.children=[];this.attributes={};this.onclick=null;this.id='';this.focused=false;} append(...x){this.children.push(...x)} appendChild(x){this.children.push(x)} replaceChildren(...x){this.children=[...x]} setAttribute(k,v){this.attributes[k]=v} addEventListener(_n,fn){this.onclick=fn} focus(){document.activeElement=this;this.focused=true;} }\n"
        "const document={activeElement:null,createElement:()=>new Node()};\n"
        + helpers
        + "const segment=new Node(); segment.querySelectorAll=()=>[];\n"
        + "const message={_anchor_activity_scene:{artifacts:"
        + json.dumps(artifacts)
        + "}}; _renderTurnArtifactListForMessage(message,segment,0);\n"
        + "const list=segment.children[0]; const items=list.children[0]; const toggle=list.children[1]; const stableToggle=toggle; toggle.focus(); const collapsed={items:items.children.length,toggle:toggle.textContent,expanded:toggle.attributes['aria-expanded'],type:toggle.type,controls:toggle.attributes['aria-controls'],target:items.id,focused:document.activeElement===toggle,listRole:list.attributes.role||null,itemsRole:items.attributes.role}; toggle.onclick(); const expanded={items:items.children.length,toggle:toggle.textContent,expanded:toggle.attributes['aria-expanded'],stable:toggle===stableToggle,focused:document.activeElement===toggle}; const rerenderSegment=new Node(); rerenderSegment.querySelectorAll=()=>[]; _renderTurnArtifactListForMessage(message,rerenderSegment,0); const rerenderToggle=rerenderSegment.children[0].children[1]; const rerender={items:rerenderSegment.children[0].children[0].children.length,expanded:rerenderToggle.attributes['aria-expanded']}; toggle.onclick(); console.log(JSON.stringify({collapsed,expanded,rerender,collapsedAgain:{items:items.children.length,toggle:toggle.textContent,expanded:toggle.attributes['aria-expanded'],stable:toggle===stableToggle,focused:document.activeElement===toggle}}));"
    )
    assert output == {
        "collapsed": {"items": 5, "toggle": "+7 more", "expanded": "false", "type": "button", "controls": output["collapsed"]["target"], "target": output["collapsed"]["target"], "focused": True, "listRole": None, "itemsRole": "list"},
        "expanded": {"items": 12, "toggle": "Show fewer artifacts", "expanded": "true", "stable": True, "focused": True},
        "rerender": {"items": 12, "expanded": "true"},
        "collapsedAgain": {"items": 5, "toggle": "+7 more", "expanded": "false", "stable": True, "focused": True},
    }
    assert ".turn-artifact-toggle" in styles
    assert "min-height:44px" in styles
    assert "touch-action:manipulation" in styles
    assert "overflow-wrap:anywhere" in styles


def test_artifact_sessions_bypass_html_cache_and_keep_controls_live_after_switch():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    cache_helpers = _function_source(
        "static/ui.js", "function _messagesHaveTurnArtifacts", "const _sessionHtmlCache"
    )
    artifact_helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor"
    )
    artifacts = [
        {
            "type": "artifact_reference",
            "payload": {
                "path": f"output/report-{index}.md",
                "workspace_root": "/workspace",
                "session_id": "sid-a",
                "tool_name": "patch",
                "tool_call_id": f"call-{index}",
            },
        }
        for index in range(6)
    ]
    output = _run_node(
        "const INFLIGHT={}; const cache=new Map(); const clicked=[];\n"
        "const S={session:null,messages:[]}; const t=(key,count)=>key==='turn_artifact_more'?`+${count} more`:key==='turn_artifact_show_fewer'?'Show fewer artifacts':key;\n"
        "class Node{constructor(){this.children=[];this.attributes={};this.onclick=null;this.id='';this.textContent='';} append(...x){this.children.push(...x)} appendChild(x){this.children.push(x)} replaceChildren(...x){this.children=[...x]} setAttribute(k,v){this.attributes[k]=v} addEventListener(_n,fn){this.onclick=fn}}\n"
        "const document={createElement:()=>new Node()}; const openArtifactPath=(entry)=>clicked.push(entry.path);\n"
        + cache_helpers
        + artifact_helpers
        + "const artifactMessage={role:'assistant',content:'final',_anchor_activity_scene:{artifacts:"
        + json.dumps(artifacts)
        + "}}; const plainMessage={role:'assistant',content:'plain'};\n"
        "function renderSession(sid,message){S.session={session_id:sid,workspace:'/workspace'};S.messages=[message];if(_sessionHtmlCacheEligible(sid,false,S.messages)){if(cache.has(sid))return cache.get(sid);const inert={cached:true};cache.set(sid,inert);return inert;}const segment=new Node();segment.querySelectorAll=()=>[];_renderTurnArtifactListForMessage(message,segment,0);return segment;}\n"
        "const first=renderSession('sid-a',artifactMessage);renderSession('sid-b',plainMessage);const restored=renderSession('sid-a',artifactMessage);const list=restored.children[0];const items=list.children[0];const toggle=list.children[1];items.children[0].children[0].onclick();toggle.onclick();console.log(JSON.stringify({artifactCached:cache.has('sid-a'),plainCached:cache.has('sid-b'),rerendered:first!==restored,clicked,expanded:toggle.attributes['aria-expanded'],itemCount:items.children.length}));"
    )
    assert output == {
        "artifactCached": False,
        "plainCached": True,
        "rerendered": True,
        "clicked": ["output/report-0.md"],
        "expanded": "true",
        "itemCount": 6,
    }
    assert ui.count("_sessionHtmlCacheEligible(sid,hasTransientTranscriptUi,S.messages)") == 2


def test_mobile_turn_artifact_items_meet_computed_touch_target_floor():
    """Exercise the shipped CSS in Chromium at the narrow mobile viewport."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is unavailable; run the browser-smoke environment")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        with browser:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_content(
                f"<style>{styles}</style>"
                '<div class="turn-artifact-list"><div class="turn-artifact-items">'
                '<div role="listitem"><button class="turn-artifact-item">output/report.md</button></div>'
                "</div></div>"
            )
            box = page.locator(".turn-artifact-item").bounding_box()
            assert box is not None
            assert box["height"] >= 44


def test_final_answer_artifact_entries_are_turn_owned_and_workspace_scoped():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            None,
            {"type":"artifact_reference","payload": {"path": "output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-1"}},
            {"type":"artifact_reference","payload": {"path": " report.md ", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-spaced"}},
            {"type":"artifact_reference","payload": {"path": "./output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-2"}},
            {"type":"artifact_reference","payload": {"path": "output/old-workspace.md", "workspace_root": "/workspace-a"}},
            {"type":"artifact_reference","payload": {"path": "/workspace/output/absolute.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-3"}},
            {"type":"artifact_reference","payload": {"path": "../escape.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "output\\windows.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "C:/outside/windows.md", "workspace_root": "/workspace"}},
            {"type":"artifact_reference","payload": {"path": "output/unbound.md"}},
            {"type":"wrong_type","payload": {"path":"output/untyped.md","workspace_root":"/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-4"}},
            {"type":"artifact_reference","payload": {"path":"output/invalid-type.md","workspace_root":"/workspace","session_id":22,"tool_name":"write_file","tool_call_id":"call-4"}},
            {"payload": {"path":"output/no-type.md","workspace_root":"/workspace","session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-5"}},
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }, {
        "path": " report.md ",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-spaced",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]
    assert "_attachTurnArtifactsFromToolCall(tc);" in messages
    assert "_applyToAnchor('artifact_reference'" in messages
    assert "_anchorHasArtifactReference(localId,workspaceRoot,path)" in messages
    assert "workspace_root:workspaceRoot" in messages
    assert "if(typeof _renderTurnArtifactListForMessage==='function')" in ui
    assert "_renderTurnArtifactListForMessage(msg, seg, rawIdx);" in ui
    assert "openArtifactPath(entry)" in ui
    assert "return _turnArtifactEntriesFromScene(message&&message._anchor_activity_scene);" in ui
    assert "_turn_artifacts" not in ui


def test_turn_artifact_entries_accept_top_level_session_id_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            {
                "type": "artifact_reference",
                "session_id": "sid-owner",
                "payload": {
                    "path": "output/backup-report.md",
                    "workspace_root": "/workspace",
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                },
            }
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{
        "path": "output/backup-report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]


def test_final_answer_uses_anchor_scene_artifact_refs_without_message_history_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'},messages:[{role:'assistant',content:'final'}]};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesForMessage({"
        + "_anchor_activity_scene:{artifacts:[{type:'artifact_reference',payload:{path:'output/large-worklog.md',workspace_root:'/workspace',session_id:'sid-owner',tool_name:'patch',tool_call_id:'call-2'}}]}},0)));"
    )
    assert output == [{
        "path": "output/large-worklog.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "patch",
        "tool_call_id": "call-2",
        "type": "artifact_reference",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]


def test_replay_merges_missing_artifact_into_existing_anchor_scene():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {
                        "type": "artifact_reference",
                        "payload": {"path": "output/report.md", "workspace_root": "/workspace"},
                    }
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-1",
                    "tool_name": "write_file",
                    "session_id": "sid-replay",
                },
                {
                    "path": "output/large-worklog.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-2",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ]
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["activity_rows"] == [{"type": "tool"}]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
                "session_id": "sid-replay",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_replaces_under_typed_existing_anchor_artifacts_with_transcript_descriptors():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {"type": "artifact_reference", "payload": {"path": "output/report.md", "workspace_root": "/workspace"}},
                    {
                        "type": "artifact_reference",
                        "payload": {
                            "path": "output/typed.md",
                            "workspace_root": "/workspace",
                            "tool_name": "patch",
                            "tool_call_id": "call-existing",
                        },
                    },
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-1",
                    "tool_name": "write_file",
                    "session_id": "sid-replay",
                },
                {
                    "path": "output/typed.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-2",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ],
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/typed.md",
                "workspace_root": "/workspace",
                "session_id": "sid-replay",
                "tool_name": "patch",
                "tool_call_id": "call-2",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_discards_unbacked_and_forged_client_artifacts():
    from api import routes

    base_scene = {
        "version": "activity_scene_v1",
        "activity_rows": [{"type": "tool"}],
        "artifacts": [{
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "session_id": "sid-client",
                "tool_name": "write_file",
                "tool_call_id": "forged",
            },
        }],
    }
    message = {"role": "assistant", "content": "final", "_anchor_activity_scene": base_scene}
    assert routes._attach_replayed_turn_artifacts_to_anchor_scenes([message], {0: []})[0][
        "_anchor_activity_scene"
    ]["artifacts"] == []
    foreign_session_hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        [message],
        {0: []},
        replay_source_messages=[message],
        replay_session_id="sid-server",
    )
    assert foreign_session_hydrated[0]["_anchor_activity_scene"]["artifacts"] == []

    canonical = {
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-server",
        "tool_name": "patch",
        "tool_call_id": "call-server",
    }
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        [message], {0: [canonical]}
    )
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [{
        "type": "artifact_reference",
        "payload": {**canonical, "source": "transcript_replay"},
    }]


def test_replay_preserves_only_transcript_backed_persisted_artifacts_after_workspace_move():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "patch"}},
                {"id": "call-2", "function": {"name": "patch"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps(
                {
                    "success": True,
                    "files_modified": ["/workspace-a/output/report.md"],
                }
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "name": "patch",
            "content": json.dumps(
                {
                    "success": True,
                    "files_modified": ["/workspace-a/output/ exact report.md "],
                }
            ),
        },
        {"role": "assistant", "content": "final answer"},
    ]
    current_workspace_artifacts = routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace-b",
        session_id="sid-replay",
    )
    assert current_workspace_artifacts == {}

    persisted_scene = {
        "version": "activity_scene_v1",
        "activity_rows": [{"type": "tool"}],
        "artifacts": [
            {
                "type": "artifact_reference",
                "payload": {
                    "path": "output/report.md",
                    "workspace_root": "/workspace-a",
                    "session_id": "sid-replay",
                    "tool_name": "patch",
                    "tool_call_id": "call-1",
                    "source": "live_tool_complete",
                },
            },
            {
                "type": "artifact_reference",
                "payload": {
                    "path": "output/ exact report.md ",
                    "workspace_root": "/workspace-a",
                    "session_id": "sid-replay",
                    "tool_name": "patch",
                    "tool_call_id": "call-2",
                    "source": "live_tool_complete",
                },
            },
            {
                "type": "artifact_reference",
                "payload": {
                    "path": "output/forged.md",
                    "workspace_root": "/workspace-a",
                    "session_id": "sid-replay",
                    "tool_name": "patch",
                    "tool_call_id": "forged",
                    "source": "live_tool_complete",
                },
            },
        ],
    }
    window = [{**messages[-1], "_anchor_activity_scene": persisted_scene}]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        window,
        current_workspace_artifacts,
        message_offset=4,
        replay_source_messages=messages,
        replay_session_id="sid-replay",
    )

    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace-a",
                "session_id": "sid-replay",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/ exact report.md ",
                "workspace_root": "/workspace-a",
                "session_id": "sid-replay",
                "tool_call_id": "call-2",
                "tool_name": "patch",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_clears_same_session_persisted_artifacts_without_transcript_proof(
    monkeypatch,
):
    """A replay miss must erase even a same-session persisted artifact projection."""
    from api import routes

    session_id = "sid-replay"
    messages = [
        {"role": "user", "content": "write the report"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps(
                {"success": True, "files_modified": ["/old-workspace/output/report.md"]}
            ),
        },
        {"role": "assistant", "content": "final answer"},
    ]
    persisted_artifacts = [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/old-workspace",
                "session_id": session_id,
                "tool_name": "patch",
                "tool_call_id": "call-1",
                "source": "live_tool_complete",
            },
        }
    ]
    window = [
        {
            **messages[-1],
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": persisted_artifacts,
            },
        }
    ]
    monkeypatch.setattr(routes, "_final_turn_artifact_paths", lambda *_args, **_kwargs: {})

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        window,
        {},
        message_offset=3,
        replay_source_messages=messages,
        replay_session_id=session_id,
    )

    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == []


def test_historical_artifact_replay_fails_closed_before_work_when_response_root_budget_exceeded(
    monkeypatch,
):
    """A many-root response must not multiply full-transcript replay work."""
    from api import routes

    session_id = "sid-many-roots"
    messages = []
    expected_by_root = {}
    for idx in range(256):
        root = f"/workspace-{idx}"
        descriptor = {
            "path": f"output/report-{idx}.md",
            "workspace_root": root,
            "session_id": session_id,
            "tool_name": "patch",
            "tool_call_id": f"call-{idx}",
        }
        expected_by_root[root] = {idx: [descriptor]}
        messages.append(
            {
                "role": "assistant",
                "content": f"final {idx}",
                "_anchor_activity_scene": {
                    "version": "activity_scene_v1",
                    "activity_rows": [{"type": "tool"}],
                    "artifacts": [
                        {
                            "type": "artifact_reference",
                            "payload": {**descriptor, "source": "live_tool_complete"},
                        }
                    ],
                },
            }
        )

    replay_calls = []

    def counted_replay(_messages, *, workspace_root, session_id=""):
        replay_calls.append((workspace_root, session_id))
        return expected_by_root.get(workspace_root, {})

    monkeypatch.setattr(routes, "_final_turn_artifact_paths", counted_replay)

    current_descriptor = {
        "path": "output/current.md",
        "workspace_root": "/current-workspace",
        "session_id": session_id,
        "tool_name": "patch",
        "tool_call_id": "call-current",
    }

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {0: [current_descriptor]},
        replay_source_messages=messages,
        replay_session_id=session_id,
    )

    assert replay_calls == []
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {**current_descriptor, "source": "transcript_replay"},
        }
    ]
    assert all(
        message["_anchor_activity_scene"]["artifacts"] == []
        for message in hydrated[1:]
    )


def test_replay_replaces_wrong_session_existing_anchor_artifacts_with_transcript_descriptors():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {
                        "type": "artifact_reference",
                        "payload": {
                            "path": "output/report.md",
                            "workspace_root": "/workspace",
                            "tool_name": "patch",
                            "tool_call_id": "call-existing",
                            "session_id": "sid-old",
                        },
                    },
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {
                    "path": "output/report.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-replay",
                    "tool_name": "patch",
                    "session_id": "sid-replay",
                },
            ],
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_name": "patch",
                "tool_call_id": "call-replay",
                "session_id": "sid-replay",
                "source": "transcript_replay",
            },
        }
    ]


def test_replay_collision_controls_feed_real_renderer_and_click_current_owner():
    from api import routes

    descriptor = {
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "tool_call_id": "call-replay",
        "tool_name": "patch",
        "session_id": "sid-replay",
    }
    incumbents = [
        {"type": "artifact_reference", "payload": {"path": descriptor["path"], "workspace_root": descriptor["workspace_root"]}},
        {"type": "wrong_type", "payload": {**descriptor}},
        {"type": "artifact_reference", "payload": {**descriptor, "session_id": "sid-old"}},
        {"source_event_type": "artifact_reference", "session_id": "sid-replay", "payload": {**descriptor}},
        {"type": "artifact_reference", "payload": {**descriptor}},
    ]
    for incumbent in incumbents:
        hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
            [{
                "role": "assistant",
                "content": "final answer",
                "_anchor_activity_scene": {
                    "version": "activity_scene_v1",
                    "activity_rows": [],
                    "artifacts": [incumbent],
                },
            }],
            {0: [descriptor]},
        )
        scene = hydrated[0]["_anchor_activity_scene"]
        helpers = _function_source(
            "static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor"
        )
        output = _run_node(
            "const S={session:{workspace:'/workspace',session_id:'sid-replay'}};\n"
            "const clicked=[]; const openArtifactPath=(entry)=>clicked.push(entry);\n"
            "const document={createElement:()=>({className:'',title:'',type:'',innerHTML:'',children:[],append(...x){this.children.push(...x)},appendChild(x){this.children.push(x)},replaceChildren(...x){this.children=[...x]},setAttribute(){},addEventListener(_name,fn){this.onclick=fn}})};\n"
            + helpers
            + "const segment={children:[],querySelectorAll:()=>[],appendChild(node){this.children.push(node)}};\n"
            + "const message={_anchor_activity_scene:"
            + json.dumps(scene)
            + "};\n"
            + "_renderTurnArtifactListForMessage(message,segment,0);\n"
            + "segment.children[0].children[0].children[0].children[0].onclick();\n"
            + "console.log(JSON.stringify({entries:_turnArtifactEntriesFromScene(message._anchor_activity_scene),clicked}));"
        )
        expected = {
            "path": "output/report.md",
            "workspace_root": "/workspace",
            "session_id": "sid-replay",
            "tool_name": "patch",
            "tool_call_id": "call-replay",
            "type": "artifact_reference",
            "owner": {"session_id": "sid-replay", "workspace_root": "/workspace"},
        }
        assert output == {"entries": [expected], "clicked": [expected]}


def test_paginated_session_response_keeps_paired_landed_turn_artifacts():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/large-worklog.md"]}),
        },
        {"role": "assistant", "content": "working"},
        {"role": "tool", "name": "read_file", "content": "ignored"},
        {"role": "assistant", "content": "final answer"},
    ]

    paths_by_final_index = routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-work",
    )
    window, offset = routes._message_window_for_display(messages, msg_limit=1)
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(window, paths_by_final_index, message_offset=offset)

    assert offset == 5
    assert hydrated[0]["_anchor_activity_scene"]["version"] == "activity_scene_v1"
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "session_id": "sid-work",
                "source": "transcript_replay",
            },
        }
    ]


def test_anthropic_tool_use_results_replay_as_turn_artifacts_without_splitting_turn():
    from api import routes

    messages = [
        {"role": "user", "content": "write both files"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "working"},
                {
                    "type": "tool_use",
                    "id": "toolu-write",
                    "name": "write_file",
                    "input": {"path": "output/report.md"},
                },
                {
                    "type": "tool_use",
                    "tool_use_id": "toolu-patch",
                    "tool_name": "patch",
                    "input": {"patch": "..."},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-write",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "bytes_written": 8,
                                    "resolved_path": "/workspace/output/report.md",
                                }
                            ),
                        }
                    ],
                }
            ],
        },
        {
            "role": "tool",
            "tool_use_id": "toolu-patch",
            "content": json.dumps(
                {
                    "success": True,
                    "files_modified": ["/workspace/output/notes.md"],
                }
            ),
        },
        {"role": "assistant", "content": [{"type": "text", "text": "final answer"}]},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-anthropic",
    ) == {
        4: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "toolu-write",
                "tool_name": "write_file",
                "session_id": "sid-anthropic",
            },
            {
                "path": "output/notes.md",
                "workspace_root": "/workspace",
                "tool_call_id": "toolu-patch",
                "tool_name": "patch",
                "session_id": "sid-anthropic",
            },
        ]
    }


def test_anthropic_artifact_replay_still_rejects_unpaired_and_ambiguous_evidence():
    from api import routes

    success_result = json.dumps(
        {"bytes_written": 8, "resolved_path": "/workspace/output/report.md"}
    )
    cases = [
        [
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu-dup", "name": "write_file"},
                    {"type": "tool_use", "id": "toolu-dup", "name": "write_file"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu-dup", "content": success_result}
                ],
            },
            {"role": "assistant", "content": "final"},
        ],
        [
            {"role": "user", "content": "write"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu-orphan", "content": success_result}
                ],
            },
            {"role": "assistant", "content": "final"},
        ],
        [
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu-twice", "name": "write_file"}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu-twice", "content": success_result},
                    {"type": "tool_result", "tool_use_id": "toolu-twice", "content": success_result},
                ],
            },
            {"role": "assistant", "content": "final"},
        ],
        [
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu-mixed", "name": "write_file"}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu-mixed", "content": success_result},
                    {"type": "text", "text": "Now answer a different question"},
                ],
            },
            {"role": "assistant", "content": "new answer"},
        ],
    ]

    for messages in cases:
        assert routes._final_turn_artifact_paths(
            messages,
            workspace_root="/workspace",
            session_id="sid-anthropic",
        ) == {}


def test_replay_rejects_failed_unpaired_duplicate_and_mismatched_writes():
    from api import routes

    messages = [
        {"role": "user", "content": "write"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-failed", "function": {"name": "write_file"}},
                {"id": "call-mismatch", "function": {"name": "write_file"}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-conflict", "function": {"name": "write_file"}},
                {"id": "call-patch", "function": {"name": "patch"}},
                {"id": "call-result", "function": {"name": "write_file"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-failed",
            "name": "write_file",
            "content": json.dumps({"error": "denied", "resolved_path": "/workspace/output/failed.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-mismatch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/mismatch.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"error": "first failed", "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 4, "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-orphan",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 2, "resolved_path": "/workspace/output/orphan.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-conflict",
            "name": "write_file",
            "content": json.dumps({"error": "displayed error", "resolved_path": "/workspace/output/conflict.md"}),
            "result": json.dumps({"bytes_written": 8, "resolved_path": "/workspace/output/conflict.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-patch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/report.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-result",
            "name": "write_file",
            "content": "Wrote /workspace/output/result-field.md",
            "result": json.dumps({"bytes_written": 11, "resolved_path": "/workspace/output/result-field.md"}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-default",
    ) == {
        10: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-patch",
                "tool_name": "patch",
                "session_id": "sid-default",
            },
            {
                "path": "output/result-field.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-result",
                "tool_name": "write_file",
                "session_id": "sid-default",
            }
        ]
    }


def test_final_turn_artifact_paths_treats_ambiguous_call_sequences_as_invalid():
    from api import routes

    messages = [
        {"role": "user", "content": "write"},
        {"role": "assistant", "content": "prepare"},
        {
            "role": "tool",
            "tool_call_id": "call-pre",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 1, "resolved_path": "/workspace/output/predecl.md"}),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-missing", "function": {}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-mismatch", "function": {"name": "write_file"}},
                {"id": "call-dupres", "function": {"name": "write_file"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-missing",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 2, "resolved_path": "/workspace/output/missing.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 3, "resolved_path": "/workspace/output/dupe-first.md"}),
        },
        {"role": "assistant", "content": "middle"},
        {
            "role": "tool",
            "tool_call_id": "call-dupres",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 4, "resolved_path": "/workspace/output/dupres-first.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupres",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 5, "resolved_path": "/workspace/output/dupres-second.md"}),
        },
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-mismatch", "function": {"name": "patch"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-mismatch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/mismatch.md"]}),
        },
        {"role": "assistant", "tool_calls": [{"id": "call-valid", "function": {"name": "write_file"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-valid",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 6, "resolved_path": "/workspace/output/valid.md"}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-ambiguous",
    ) == {
        13: [
            {
                "path": "output/valid.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-valid",
                "tool_name": "write_file",
                "session_id": "sid-ambiguous",
            },
        ]
    }


def test_final_turn_artifact_projection_keeps_session_id_for_replay():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/report.md"]}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(
        messages,
        workspace_root="/workspace",
        session_id="sid-projection",
    ) == {
        3: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "session_id": "sid-projection",
            }
        ],
    }


def test_landed_artifact_descriptors_use_actual_hermes_success_shapes():
    from api.turn_artifacts import landed_artifact_descriptors

    assert landed_artifact_descriptors(
        "write_file",
        {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
        workspace_root="/workspace",
        tool_call_id="call-write",
    ) == [
        {
            "path": "output/report.md",
            "workspace_root": "/workspace",
            "tool_call_id": "call-write",
            "tool_name": "write_file",
        }
    ]
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"error": "permission denied", "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"bytes_written": 3, "resolved_path": "/workspace-a/output/report.md"},
            workspace_root="/workspace-b",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "mcp_filesystem_write_file",
            {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-plugin",
        )
        == []
    )


def test_live_stream_completion_uses_landed_artifact_descriptors():
    streaming = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    start = streaming.index("def on_tool_complete")
    end = streaming.index("# Mirror the todo tool", start)
    body = streaming[start:end]
    assert "landed_artifact_descriptors(" in body
    assert "'artifacts': landed_artifacts" in body
    assert "'is_error': tool_result_is_error(function_result)" in body
    assert "'is_error': False" not in body


def test_compression_settlement_rebinds_artifacts_only_for_the_completed_workspace():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    settle = _function_source(
        "static/messages.js",
        "function _settleTurnArtifactSceneForSession",
        "function _anchorSceneMessageOffsetForPersist",
    )
    persistence = _function_source(
        "static/messages.js",
        "function _anchorSceneMessageOffsetForPersist",
        "function _anchorSceneHasWorklogWorthyRows",
    )
    output = _run_node(
        "const activeSid='sid-parent';const streamId='stream-1';let _oldestIdx=0;const calls=[];const _anchorSceneMessageRef=()=>({id:'message-1'});const api=(_path,options)=>{calls.push(JSON.parse(options.body));return Promise.resolve({});};\n"
        + settle
        + persistence
        + "const scene={version:'activity_scene_v1',artifacts:[{type:'artifact_reference',source_event_type:'artifact_reference',session_id:'sid-parent',payload:{path:'output/report.md',workspace_root:'/workspace',session_id:'sid-parent',tool_name:'patch',tool_call_id:'call-1',source:'live_tool_complete'}}]};\n"
        "const same=_settleTurnArtifactSceneForSession(scene,{session_id:'sid-child',workspace:'/workspace'});const changed=_settleTurnArtifactSceneForSession(scene,{session_id:'sid-other',workspace:'/other'});_persistSettledAnchorScene({role:'assistant'},same,0,{session_id:'sid-child',workspace:'/workspace'});console.log(JSON.stringify({same,changed,persisted:calls[0]}));"
    )
    artifact = output["same"]["artifacts"][0]
    assert artifact["session_id"] == "sid-child"
    assert artifact["payload"]["session_id"] == "sid-child"
    assert artifact["payload"]["workspace_root"] == "/workspace"
    assert artifact["payload"]["owner"] == {
        "session_id": "sid-child",
        "workspace_root": "/workspace",
    }
    assert output["changed"]["artifacts"] == []
    assert output["persisted"]["session_id"] == "sid-child"
    assert (
        "_attachProjectedAnchorSceneToLastAssistant(S.messages, null, null, completedSession);"
        in messages
    )


def test_artifact_open_expands_a_closed_workspace_preview_before_loading_file():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("async function openArtifactPath(path)")
    end = workspace.index("// ── Workspace file-tree", start)
    body = workspace[start:end]
    assert "ensureWorkspacePreviewVisible()" in body
    assert body.index("ensureWorkspacePreviewVisible()") < body.index("openFile(rel,{owner,_openGeneration:generation,_preserveArtifactPath:true});")
