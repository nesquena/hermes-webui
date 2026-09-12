"""Regression coverage for fail-closed artifact workspace classification (#7239)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_fn(name):
    marker = f"function {name}("
    start = WORKSPACE_JS.index(marker)
    if start >= 6 and WORKSPACE_JS[start - 6 : start] == "async ":
        start -= 6
    brace = WORKSPACE_JS.index("{", start)
    depth = 0
    for pos in range(brace, len(WORKSPACE_JS)):
        if WORKSPACE_JS[pos] == "{":
            depth += 1
        elif WORKSPACE_JS[pos] == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _common_js(*functions):
    functions = list(functions)
    if "_classifyArtifactPath" in functions and "_normalizeExplicitArtifactPath" not in functions:
        functions.insert(0, "_normalizeExplicitArtifactPath")
    constants = re.findall(r"const (?:ARTIFACT_IGNORE_RE|ARTIFACT_MUTATION_TOOLS) = .*?;", WORKSPACE_JS)
    return "\n".join(constants) + "\n" + "\n".join(_extract_fn(name) for name in functions)


def _node_json(script, *args):
    result = subprocess.run(
        [NODE, "-e", script, *[json.dumps(arg) for arg in args]],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _i18n_vm_script(body):
    return (
        "const vm = require('vm');\n"
        "const fs = require('fs');\n"
        f"const source = fs.readFileSync({json.dumps(str(REPO / 'static' / 'i18n.js'))}, 'utf8');\n"
        "const storage = new Map();\n"
        "const context = {\n"
        "  console,\n"
        "  localStorage: {\n"
        "    getItem: key => storage.get(key) ?? null,\n"
        "    setItem: (key, value) => storage.set(key, value),\n"
        "  },\n"
        "  document: {documentElement: {lang: ''}, querySelectorAll: () => []},\n"
        "};\n"
        "vm.createContext(context);\n"
        "vm.runInContext(source, context);\n"
        "vm.runInContext(\"globalThis.__i18n = {locales: Object.keys(LOCALES), t, setLocale};\", context);\n"
        f"vm.runInContext({json.dumps(f'globalThis.__result = ({body});')}, context);\n"
        "process.stdout.write(JSON.stringify(context.__result));"
    )


_MISSING = object()


def _classify(paths, workspace=_MISSING):
    if workspace is _MISSING:
        script = _common_js("_normalizeExplicitArtifactPath", "_classifyArtifactPath") + "\nconst input=JSON.parse(process.argv[1]); process.stdout.write(JSON.stringify(input.map(p=>_classifyArtifactPath(p, undefined))));"
        return _node_json(script, paths)
    script = _common_js("_normalizeExplicitArtifactPath", "_classifyArtifactPath") + "\nconst input=JSON.parse(process.argv[1]); const ws=JSON.parse(process.argv[2]); process.stdout.write(JSON.stringify(input.map(p=>_classifyArtifactPath(p, ws))));"
    return _node_json(script, paths, workspace)


def test_reported_absolute_artifact_is_display_only_and_network_silent():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_classifyArtifactCandidate", "_artifactCandidatesFromText", "_artifactCandidatesFromToolCall", "collectSessionArtifacts", "renderSessionArtifacts", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const statuses=[]; const root={innerHTML:'',};
const S={session:{workspace:'/home/hermesuser/workspace',session_id:'s'},toolCalls:[{name:'write_file',args:{path:'/home/hermesuser/.hermes/shared/hermes-profile-setup-credentials.md'}}],messages:[]};
const $=id=>id==='workspaceArtifacts'?root:null; const esc=s=>String(s); const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>{calls.push(url);return {entries:[]}}; const setStatus=s=>statuses.push(s); const openFile=()=>calls.push('open');
renderSessionArtifacts(); openArtifactPath('/home/hermesuser/.hermes/shared/hermes-profile-setup-credentials.md').then(()=>process.stdout.write(JSON.stringify({html:root.innerHTML,calls,statuses})));'''
    out = _node_json(script)
    assert "<button" not in out["html"]
    assert "workspace_artifact_outside_workspace" in out["html"]
    assert out["calls"] == []
    assert out["statuses"] == ["workspace_artifact_outside_workspace"]


def test_posix_containment_matrix():
    rows = _classify([
        "/workspace/report.md", "/workspace-other/report.md", "/Workspace/report.md",
        "~/shared/file.md", "../secret.md", "./relative.md", "report.md",
    ], "/workspace")
    assert [row["kind"] for row in rows] == [
        "workspace-contained", "outside", "outside", "unsupported", "unsupported",
        "workspace-relative", "workspace-relative",
    ]
    assert rows[0]["openPath"] == "report.md"
    assert rows[1]["openPath"] is None
    assert rows[5]["openPath"] == "relative.md"


@pytest.mark.parametrize("workspace", ["", None, "   ", 17])
def test_safe_relative_paths_do_not_require_workspace(workspace):
    rows = _classify([
        "reports/output.md", "./reports/output.md", r"reports\output.md", "README",
    ], workspace)
    assert [row["kind"] for row in rows] == ["workspace-relative"] * 4
    assert [row["openPath"] for row in rows] == [
        "reports/output.md", "reports/output.md", "reports/output.md", "README",
    ]


def test_safe_relative_paths_do_not_require_workspace_when_omitted():
    rows = _classify(["reports/output.md", "README"])
    assert [row["kind"] for row in rows] == ["workspace-relative", "workspace-relative"]
    assert [row["openPath"] for row in rows] == ["reports/output.md", "README"]


def test_explicit_root_names_and_ignored_directories_bypass_discovery_filters():
    rows = _classify(["README", "LICENSE", "Makefile", "node_modules/file.js"], "")
    assert [row["kind"] for row in rows] == ["workspace-relative"] * 4

    script = _common_js(
        "_sanitizeArtifactPath",
        "_normalizeExplicitArtifactPath",
        "_classifyArtifactPath",
        "_classifyArtifactCandidate",
        "_artifactCandidatesFromToolCall",
        "_artifactCandidatesFromText",
        "collectSessionArtifacts",
    ) + r'''
const S={session:{workspace:''},toolCalls:[
  {name:'write_file',args:{path:'README'}},
  {name:'write_file',args:{path:'node_modules/file.js'}},
  {name:'write_file',args:{path:'reports/output.md'}}
],messages:[]};
process.stdout.write(JSON.stringify(collectSessionArtifacts().map(item=>item.path)));'''
    assert _node_json(script) == ["reports/output.md"]


def test_explicit_safety_rejects_unsafe_path_forms_and_keeps_long_names():
    long_path = "a" * 246
    rows = _classify([
        "https://example.test/report.md", "//server/share/report.md",
        "C:relative.md", "bad\x00name.md", long_path,
    ], "")
    assert [row["kind"] for row in rows[:4]] == ["unsupported"] * 4
    assert rows[4] == {
        "kind": "workspace-relative",
        "displayPath": long_path,
        "dedupeKey": long_path,
        "openPath": long_path,
    }


def test_collection_dedupes_relative_and_workspace_absolute_aliases():
    script = _common_js(
        "_sanitizeArtifactPath",
        "_classifyArtifactPath",
        "_classifyArtifactCandidate",
        "_artifactCandidatesFromToolCall",
        "_artifactCandidatesFromText",
        "collectSessionArtifacts",
    ) + r'''
const S={session:{workspace:'/workspace'},toolCalls:[
  {name:'write_file',args:{path:'./report.md'}},
  {name:'write_file',args:{path:'/workspace/report.md'}},
  {name:'write_file',args:{path:'report.md'}}
],messages:[]};
process.stdout.write(JSON.stringify(collectSessionArtifacts().map(item=>item.classification.dedupeKey)));'''
    assert _node_json(script) == ["report.md"]


def test_windows_containment_matrix():
    rows = _classify([
        r"d:/proj/src/report.pdf", r"D:\Proj\report.pdf", r"E:\Proj\file.md",
        r"C:\work-old\report.md", r"c:/WORK/report.md", r"C:relative.md",
    ], r"D:\Proj")
    assert [row["kind"] for row in rows] == [
        "workspace-contained", "workspace-contained", "outside", "outside", "outside", "unsupported",
    ]
    assert rows[0]["openPath"] == "src/report.pdf"
    assert rows[1]["openPath"] == "report.pdf"


def test_explicit_root_filename_is_contained_with_a_known_workspace():
    posix = _classify(["/workspace/README"], "/workspace")[0]
    windows = _classify([r"D:\Proj\README"], r"D:\Proj")[0]
    assert posix["kind"] == "workspace-contained"
    assert posix["openPath"] == "README"
    assert windows["kind"] == "workspace-contained"
    assert windows["openPath"] == "README"


def test_repeated_absolute_separators_keep_display_and_open_paths_distinct():
    posix = _classify(["/workspace//report.md"], "/workspace")[0]
    windows = _classify([r"D:\Proj\\report.md"], r"D:\Proj")[0]

    assert posix["kind"] == "workspace-contained"
    assert posix["displayPath"] == "/report.md"
    assert posix["openPath"] == "report.md"
    assert posix["dedupeKey"] == "report.md"
    assert windows["kind"] == "workspace-contained"
    assert windows["openPath"] == "report.md"
    assert windows["dedupeKey"] == "report.md"


def test_windows_relative_and_canonical_boundary_matrix():
    rows = _classify([
        "./report.md", "Report.md", "d:/proj/report.md",
        "d:/proj/dir/./report.md", "d:/proj/foo/..", "C:/report.md",
    ], r"D:\Proj")
    assert [row["kind"] for row in rows] == [
        "workspace-relative", "workspace-relative", "workspace-contained",
        "workspace-contained", "unsupported", "outside",
    ]
    assert rows[0]["openPath"] == "report.md"
    assert rows[1]["dedupeKey"] == "report.md"
    assert rows[2]["dedupeKey"] == "report.md"
    assert rows[3]["openPath"] == "dir/report.md"

    root_rows = _classify(["/report.md", "report.md", "C:/report.md"], "/")
    assert [row["kind"] for row in root_rows] == [
        "workspace-contained", "workspace-relative", "outside",
    ]


def test_direct_outside_open_call_has_no_side_effects():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const S={session:{workspace:'/workspace',session_id:'s'}}; const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>calls.push(url); const setStatus=s=>calls.push(s); const openFile=()=>calls.push('open');
openArtifactPath('/workspace-other/report.md').then(()=>process.stdout.write(JSON.stringify(calls)));'''
    assert _node_json(script) == ["workspace_artifact_outside_workspace"]


@pytest.mark.parametrize("workspace", ["/workspace", "", None])
def test_root_relative_open_lists_dot_then_opens_readme(workspace):
    script = _common_js(
        "_normalizeExplicitArtifactPath",
        "_classifyArtifactPath",
        "_workspacePathExists",
        "openArtifactPath",
    ) + f'''
const calls=[]; const S={{session:{{workspace:{json.dumps(workspace)},session_id:'s'}}}}; const t=k=>k;
const switchWorkspacePanelTab=tab=>calls.push({{tab}});
const api=async url=>{{
  calls.push({{api:url}});
  const path=decodeURIComponent(url.match(/[?&]path=([^&]*)/)[1]);
  return {{entries:path==='.'?[{{name:'README',path:'README'}}]:[]}};
}};
const setStatus=s=>calls.push({{status:s}}); const openFile=path=>calls.push({{open:path}});
openArtifactPath('README').then(()=>process.stdout.write(JSON.stringify(calls)));'''
    assert _node_json(script) == [
        {"tab": "files"},
        {"api": "/api/list?session_id=s&path=."},
        {"open": "README"},
    ]


@pytest.mark.parametrize("path", ["/workspace/report.md", r"D:\\workspace\\report.md"])
@pytest.mark.parametrize("workspace", ["", None])
def test_absolute_open_without_workspace_fails_before_side_effects(path, workspace):
    script = _common_js(
        "_normalizeExplicitArtifactPath",
        "_classifyArtifactPath",
        "_workspacePathExists",
        "openArtifactPath",
    ) + f'''
const calls=[]; const S={{session:{{workspace:{json.dumps(workspace)},session_id:'s'}}}}; const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>calls.push(url);
const setStatus=s=>calls.push(s); const openFile=()=>calls.push('open');
openArtifactPath({json.dumps(path)}).then(()=>process.stdout.write(JSON.stringify(calls)));'''
    assert _node_json(script) == ["workspace_artifact_unsupported"]


def test_inside_missing_artifact_preserves_failure_status():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const S={session:{workspace:'/workspace',session_id:'s'}}; const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>{calls.push(url);return {entries:[]}}; const setStatus=s=>calls.push(s); const openFile=()=>calls.push('open');
openArtifactPath('/workspace/missing.md').then(()=>process.stdout.write(JSON.stringify(calls)));'''
    calls = _node_json(script)
    assert calls[0] == "tab"
    assert calls[1].startswith("/api/list?")
    assert calls[-1] == "file_open_failed"
    assert "open" not in calls


def test_artifact_boundary_keys_are_english_fallback_owned_and_runtime_covered():
    blocks = list(re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", I18N_JS, re.MULTILINE))
    assert len(blocks) == 15
    end = I18N_JS.index("\n};", blocks[-1].start())
    locale_blocks = {
        match.group(1).strip("'"): I18N_JS[
            match.start() : (blocks[index + 1].start() if index + 1 < len(blocks) else end)
        ]
        for index, match in enumerate(blocks)
    }
    keys = ("workspace_artifact_outside_workspace", "workspace_artifact_unsupported")
    for key in keys:
        assert len(re.findall(rf"^    {key}:\s*", locale_blocks["en"], re.MULTILINE)) == 1
        for locale, block in locale_blocks.items():
            if locale != "en":
                assert not re.search(rf"^    {key}:\s*", block, re.MULTILINE), locale

    out = _node_json(_i18n_vm_script(
        "(() => {\n"
        "  const keys = ['workspace_artifact_outside_workspace', 'workspace_artifact_unsupported'];\n"
        "  __i18n.setLocale('en');\n"
        "  const english = keys.map(key => __i18n.t(key));\n"
        "  const fallbacks = {};\n"
        "  const activeSpeech = {};\n"
        "  for (const locale of __i18n.locales.filter(locale => locale !== 'en')) {\n"
        "    __i18n.setLocale(locale);\n"
        "    activeSpeech[locale] = document.documentElement.lang;\n"
        "    fallbacks[locale] = keys.map(key => __i18n.t(key));\n"
        "  }\n"
        "  __i18n.setLocale('en');\n"
        "  return {locales: __i18n.locales, english, fallbacks, activeSpeech, unknown: __i18n.t('workspace_artifact_missing_control')};\n"
        "})()"
    ))
    expected_locales = ["en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko", "fr", "cs", "tr", "pl", "vi"]
    assert out["locales"] == expected_locales
    assert out["english"] == [
        "Outside the active workspace",
        "This artifact path cannot be opened from the workspace",
    ]
    assert out["unknown"] == "workspace_artifact_missing_control"
    assert set(out["fallbacks"]) == set(expected_locales[1:])
    assert all(values == out["english"] for values in out["fallbacks"].values())
    assert out["activeSpeech"] == {
        "it": "it-IT",
        "ja": "ja-JP",
        "ru": "ru-RU",
        "es": "es-ES",
        "de": "de-DE",
        "zh": "zh-CN",
        "zh-Hant": "zh-TW",
        "pt": "pt-BR",
        "ko": "ko-KR",
        "fr": "fr-FR",
        "cs": "cs-CZ",
        "tr": "tr-TR",
        "pl": "pl-PL",
        "vi": "vi-VN",
    }


def test_collection_render_and_open_route_through_classifier():
    assert WORKSPACE_JS.count("function _classifyArtifactPath(") == 1
    assert "_classifyArtifactCandidate(path, S.session && S.session.workspace)" in WORKSPACE_JS
    assert "_classifyArtifactPath(path, S.session && S.session.workspace)" in WORKSPACE_JS
    render = _extract_fn("renderSessionArtifacts")
    assert "classification.kind" in render
    assert "onclick=\"openArtifactPath" in render
    assert "startsWith(normWs)" not in render
    opener = _extract_fn("openArtifactPath")
    assert opener.index("_classifyArtifactPath") < opener.index("switchWorkspacePanelTab")
    assert "_workspacePathExists(rel)" in opener
