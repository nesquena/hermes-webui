"""#7685 Tasks > Scripts subtab, UX review round 5 (release gate 2026-10-29).

Fable asked for three changes before the owner's sign-off:
1. 44 px tab rows on coarse pointers (the strip measured 32 px at 390 px).
2. Reuse the workspace panel's Files/Artifacts pill-tab idiom: no uppercase
   label that outranks the 11 px panel head.
3. The shared Refresh button's tooltip and aria-label follow the active tab.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", STYLE_CSS)
    assert m, f"rule not found: {selector}"
    return m.group(1)


def test_subtab_buttons_are_44px_on_coarse_pointers() -> None:
    assert re.search(
        r"@media \(pointer:coarse\)\{\.tasks-subtab-btn\{[^}]*min-height:44px", STYLE_CSS
    ), "the Jobs/Scripts tabs need a 44 px coarse-pointer row"


def test_subtab_buttons_use_the_workspace_pill_tab_idiom() -> None:
    btn = _rule(".tasks-subtab-btn")
    assert "text-transform:uppercase" not in btn
    assert "font-weight:600" not in btn
    ws = _rule(".workspace-panel-tab")
    for decl in ("border-radius:7px", "font-size:12px", "flex:1"):
        assert decl in ws and decl in btn, decl
    assert "background:var(--surface-subtle)" in _rule(".tasks-subtab-btn.active")


def _extract(marker: str) -> str:
    start = PANELS_JS.find(marker)
    assert start >= 0, marker
    i = PANELS_JS.find("{", start)
    depth = 0
    while True:
        ch = PANELS_JS[i]
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            return PANELS_JS[start : i + 1]
        i += 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_refresh_tooltip_and_label_follow_the_active_tab() -> None:
    harness = """
const els = {};
function mk(id){ const a={}; const cls=new Set();
  return els[id]={ id, dataset:{}, hidden:false, textContent:'',
    setAttribute:(k,v)=>{a[k]=String(v);}, getAttribute:(k)=>a[k],
    classList:{ toggle:(c,on)=>{ on?cls.add(c):cls.delete(c); } } }; }
['panelTasks','tasksPanelTitle','tasksTabJobs','tasksTabScripts','cronList','scriptsList','cronRefreshBtn'].forEach(mk);
function $(id){ return els[id]; }
function t(k){ return k; }
let _currentTasksSubtab='jobs';
let scriptsLoads=0; function loadScriptsList(){ scriptsLoads++; }
""" + _extract("function switchTasksSubtab(") + """
const b = els.cronRefreshBtn;
switchTasksSubtab('scripts');
console.log(b.getAttribute('data-tooltip') + '|' + b.getAttribute('aria-label'));
switchTasksSubtab('jobs');
console.log(b.getAttribute('data-tooltip') + '|' + b.getAttribute('aria-label'));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(harness)
        path = Path(fh.name)
    try:
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines() == [
        "Refresh script list|Refresh script list",
        "Refresh job list|Refresh job list",
    ]


def test_initial_refresh_label_matches_the_default_jobs_tab() -> None:
    m = re.search(r'<button[^>]*id="cronRefreshBtn"[^>]*>', INDEX_HTML)
    assert m and 'data-tooltip="Refresh job list"' in m.group(0)
    assert 'aria-label="Refresh job list"' in m.group(0)


def test_panel_head_tooltips_anchor_right_so_the_panel_edge_does_not_clip_them() -> None:
    # 'Refresh script list' centred under the button ran 19 px past the
    # panel's overflow edge and rendered as 'Refresh script lis' (UX round 6).
    # The right-anchored variant grows leftward, as the New conversation
    # button's does.
    for marker in ('id="cronRefreshBtn"', 'onclick="openCronCreate()"'):
        m = re.search(r'<button[^>]*' + re.escape(marker) + r'[^>]*>', INDEX_HTML)
        assert m, marker
        assert "has-tooltip--bottom-right" in m.group(0), marker

