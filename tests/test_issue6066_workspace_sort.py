"""Focused regression coverage for workspace sorting and birthtime metadata."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from api import workspace as workspace_api


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_list_dir_emits_birthtime_ns(tmp_path):
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    entries = workspace_api.list_dir(tmp_path, ".")
    assert {entry["type"] for entry in entries} == {"file", "dir"}
    assert all("birthtime_ns" in entry for entry in entries)
    assert all(isinstance(entry["birthtime_ns"], (int, type(None))) for entry in entries)


def test_birthtime_ns_platform_matrix(monkeypatch):
    birthtime_ns = getattr(workspace_api, "_birthtime_ns", None)
    assert birthtime_ns is not None, "_birthtime_ns helper is missing"
    class Stat:
        pass

    modern = Stat()
    modern.st_birthtime_ns = 123
    assert birthtime_ns(modern) == 123
    mac = Stat()
    mac.st_birthtime = 1.5
    assert birthtime_ns(mac) == 1_500_000_000
    windows = Stat()
    windows.st_ctime_ns = 456
    monkeypatch.setattr("api.workspace.sys.platform", "win32")
    assert birthtime_ns(windows) == 456
    linux = Stat()
    monkeypatch.setattr("api.workspace.sys.platform", "linux")
    assert birthtime_ns(linux) is None


def test_escape_symlink_birthtime_is_link_local(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    entry = next(item for item in workspace_api.list_dir(workspace, ".") if item["name"] == "escape.txt")
    assert entry["target_outside_workspace"] is True
    birthtime_ns = getattr(workspace_api, "_birthtime_ns", None)
    assert birthtime_ns is not None, "_birthtime_ns helper is missing"
    assert entry["birthtime_ns"] == birthtime_ns(link.lstat())
    assert "target" not in entry and "size" not in entry


def test_dir_signature_unchanged_by_birthtime(tmp_path):
    (tmp_path / "same.txt").write_text("same", encoding="utf-8")
    entries = workspace_api.list_dir(tmp_path, ".")
    expected = hashlib.sha256(
        json.dumps(
            [
                {
                    "name": entry.get("name"),
                    "path": entry.get("path"),
                    "type": entry.get("type"),
                    "is_dir": entry.get("is_dir"),
                    "size": entry.get("size"),
                    "mtime_ns": entry.get("mtime_ns"),
                    "target": entry.get("target"),
                    "target_outside_workspace": entry.get("target_outside_workspace"),
                }
                for entry in entries
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert workspace_api.dir_signature(tmp_path, ".", entries) == expected


def _run_sort_harness(body):
    start = UI_JS.index("const WORKSPACE_SORT_KEYS=")
    end = UI_JS.index("// ── Workspace preferences kebab menu", start)
    source = UI_JS[start:end]
    script = """
global.S={showHiddenWorkspaceFiles:true,workspaceSortKey:'name-asc'};
global.localStorage={getItem(key){return key==='hermes-workspace-show-hidden-files'?'1':null},setItem(){}};
global.$=()=>null;
function _visibleWorkspaceEntries(entries){return S.showHiddenWorkspaceFiles?entries:entries.filter(()=>true)}
""" + source + "\n" + body
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)


def test_sort_modified_desc_within_rank():
    result = _run_sort_harness("""
S.workspaceSortKey='modified-desc';
const input=[{name:'file-old',type:'file',mtime_ns:1},{name:'dir',type:'dir',mtime_ns:2},{name:'file-new',type:'file',mtime_ns:3},{name:'link',type:'symlink',mtime_ns:0}];
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"link\",\"dir\",\"file-new\",\"file-old\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_sort_name_desc_within_rank():
    result = _run_sort_harness("""
S.workspaceSortKey='name-desc';
const input=[{name:'a',type:'file'},{name:'dir',type:'dir'},{name:'b',type:'file'},{name:'link',type:'symlink'}];
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"link\",\"dir\",\"b\",\"a\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_name_asc_is_identity():
    result = _run_sort_harness("""
const input=[{name:'b',type:'file'},{name:'a',type:'file'}];
const out=_workspaceEntriesForRender(input);
if(out!==input||out[0]!==input[0])process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_rank_grouping_holds_all_keys():
    result = _run_sort_harness("""
const input=[{name:'s',type:'symlink',mtime_ns:1},{name:'d',type:'dir',mtime_ns:2},{name:'f',type:'file',mtime_ns:3}];
for(const key of WORKSPACE_SORT_KEYS){S.workspaceSortKey=key;const ranks=_workspaceEntriesForRender(input).map(_workspaceEntryRank);if(ranks.some((x,i)=>i&&x<ranks[i-1]))process.exit(1)}
""")
    assert result.returncode == 0, result.stderr


def test_missing_timestamps_sort_last():
    result = _run_sort_harness("""
S.workspaceSortKey='created-desc';S._workspaceBirthtimeSeen=true;
const input=[{name:'missing',type:'file',birthtime_ns:null},{name:'zero',type:'file',birthtime_ns:0},{name:'new',type:'file',birthtime_ns:2},{name:'undefined',type:'file'}];
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"new\",\"zero\",\"missing\",\"undefined\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_large_nanosecond_strings_sort_exactly():
    result = _run_sort_harness("""
S.workspaceSortKey='modified-desc';
const input=[{name:'older',type:'file',mtime_ns:'1752598800000000000'},{name:'newer',type:'file',mtime_ns:'1752598800000000001'}];
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"newer\",\"older\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_signed_nanosecond_values_sort_exactly():
    result = _run_sort_harness("""
S.workspaceSortKey='modified-desc';
const input=[{name:'negative-string',type:'file',mtime_ns:'-2'},{name:'zero',type:'file',mtime_ns:0},{name:'negative-number',type:'file',mtime_ns:-1}];
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"zero\",\"negative-number\",\"negative-string\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_signed_zero_and_leading_zero_strings_compare_equal():
    result = _run_sort_harness("""
S.workspaceSortKey='modified-desc';
const input=[{name:'zero',type:'file',mtime_ns:'0'},{name:'negative-zero',type:'file',mtime_ns:'-0'},{name:'positive-zero',type:'file',mtime_ns:'+0'},{name:'leading-zero',type:'file',mtime_ns:'000'},{name:'negative-leading-zero',type:'file',mtime_ns:'-00'}];
if(['0','-0','+0','000','-00'].some(value=>_workspaceEntryTimestampKey({mtime_ns:value},'mtime_ns')!=='0'))process.exit(1);
const out=_workspaceEntriesForRender(input).map(x=>x.name);
if(JSON.stringify(out)!=='[\"zero\",\"negative-zero\",\"positive-zero\",\"leading-zero\",\"negative-leading-zero\"]')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["", "   ", "null", "{\"k\":1}", "created"])
def test_stored_sort_key_normalization(value):
    result = _run_sort_harness(f"""
if(!_normalizeWorkspaceSortKey({json.dumps(value)})||!WORKSPACE_SORT_KEYS.includes(_normalizeWorkspaceSortKey({json.dumps(value)})))process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_prefs_indicators_or_logic():
    result = _run_sort_harness("""
const mk=()=>({hidden:true,attrs:{},setAttribute(k,v){this.attrs[k]=String(v);},removeAttribute(k){delete this.attrs[k];}});
const ind=mk(),dot=mk();
S.showHiddenWorkspaceFiles=false;S.workspaceSortKey='name-asc';
_syncWorkspacePrefsIndicators(ind,dot);
if(ind.hidden!==true||dot.hidden!==true)process.exit(1);
S.workspaceSortKey='modified-desc';
_syncWorkspacePrefsIndicators(ind,dot);
if(ind.hidden!==true||dot.hidden!==false)process.exit(1);
S.showHiddenWorkspaceFiles=true;S.workspaceSortKey='name-asc';
_syncWorkspacePrefsIndicators(ind,dot);
if(ind.hidden!==false||dot.hidden!==false)process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_created_pref_survives_unavailable():
    result = _run_sort_harness("""
const mk=()=>({hidden:true,attrs:{},setAttribute(k,v){this.attrs[k]=String(v);},removeAttribute(k){delete this.attrs[k];}});
const ind=mk(),dot=mk();
S.showHiddenWorkspaceFiles=false;S._workspaceBirthtimeSeen=false;S.workspaceSortKey='created-desc';
const input=[{name:'b',type:'file',birthtime_ns:null},{name:'a',type:'file',birthtime_ns:null}];
_noteWorkspaceBirthtimeSupport(input);
const out=_workspaceEntriesForRender(input).map(x=>x.name);
_syncWorkspacePrefsIndicators(ind,dot);
if(S._workspaceBirthtimeSeen!==false||_workspaceCreatedSortAvailable()!==false||_effectiveWorkspaceSortKey()!=='name-asc'||S.workspaceSortKey!=='created-desc'||JSON.stringify(out)!=='[\"b\",\"a\"]'||ind.hidden!==true||dot.hidden!==true)process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_birthtime_support_scope_resets_on_workspace_change():
    result = _run_sort_harness("""
const input={value:'created-desc',checked:true,disabled:false};
const row={attrs:{},classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=String(v);},querySelector(sel){return sel==='input[name="workspaceSortKey"]'?input:null;}};
_workspacePrefsMenu={querySelectorAll(sel){return sel==='.workspace-prefs-item--radio'?[row]:[];}};
S.session={workspace:'/one'};
_syncWorkspaceBirthtimeSupportScope(S.session.workspace);
_noteWorkspaceBirthtimeSupport([{name:'seen',type:'file',birthtime_ns:'1752598800000000001'}]);
if(_workspaceCreatedSortAvailable()!==true)process.exit(1);
S.session={workspace:'/two'};
_syncWorkspaceBirthtimeSupportScope(S.session.workspace);
if(S._workspaceBirthtimeSeen!==false||_workspaceCreatedSortAvailable()!==false||input.disabled!==true||row.attrs['aria-disabled']!=='true')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_birthtime_support_scope_reconciles_open_menu_metadata():
    result = _run_sort_harness("""
global.t=key=>key==='workspace_sort_created_unavailable'?'Creation unavailable':key;
global.document={createElement(){return {className:'',textContent:'',remove(){this.removed=true;},};}};
const meta={textContent:'Creation unavailable',remove(){this.removed=true;}};
const copy={children:[meta],appendChild(node){this.children.push(node);}};
const input={value:'created-desc',checked:false,disabled:true};
const row={attrs:{},classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=String(v);},querySelector(sel){
  if(sel==='input[name="workspaceSortKey"]')return input;
  if(sel==='.workspace-prefs-copy')return copy;
  if(sel==='.workspace-prefs-meta')return copy.children.find(node=>!node.removed)||null;
  return null;
}};
_workspacePrefsMenu={querySelectorAll(sel){return sel==='.workspace-prefs-item--radio'?[row]:[];}};
S._workspaceBirthtimeSeen=false;
_syncWorkspaceSortMenuState();
if(!row.querySelector('.workspace-prefs-meta')||row.querySelector('.workspace-prefs-meta').textContent!=='Creation unavailable')process.exit(1);
S._workspaceBirthtimeSeen=true;
_syncWorkspaceSortMenuState();
if(row.querySelector('.workspace-prefs-meta'))process.exit(1);
S._workspaceBirthtimeSeen=false;
_syncWorkspaceSortMenuState();
const recreated=row.querySelector('.workspace-prefs-meta');
if(!recreated||recreated.textContent!=='Creation unavailable')process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_set_workspace_sort_key_updates_open_menuitemradio_state():
    result = _run_sort_harness("""
global.renderFileTree=()=>{};
const rows=['name-asc','modified-desc'].map(value=>{
  const input={value,checked:false};
  return {_input:input,attrs:{},setAttribute(k,v){this.attrs[k]=String(v);},querySelector(sel){return sel==='input[name="workspaceSortKey"]'?input:null;}};
});
_workspacePrefsMenu={querySelectorAll(sel){return sel==='.workspace-prefs-item--radio'?rows:[];}};
S.workspaceSortKey='name-asc';
setWorkspaceSortKey('modified-desc');
if(rows[0].attrs['aria-checked']!=='false'||rows[1].attrs['aria-checked']!=='true'||rows[0]._input.checked!==false||rows[1]._input.checked!==true)process.exit(1);
""")
    assert result.returncode == 0, result.stderr


def test_sort_public_surface_text_shape():
    for key in ("WORKSPACE_SORT_KEYS", "setWorkspaceSortKey", "hermes-workspace-sort-key",
                "workspace_sort_by", "workspace_sort_name_asc", "workspace_sort_name_desc",
                "workspace_sort_created_desc", "workspace_sort_modified_desc",
                "workspace_sort_created_unavailable"):
        assert key in UI_JS or key in I18N_JS
    assert "row.setAttribute('aria-disabled',disabled?'true':'false');" in UI_JS
    assert "Number(a[field])" not in UI_JS
    assert I18N_JS.count("workspace_sort_by:") == I18N_JS.count("workspace_show_hidden_files:")
