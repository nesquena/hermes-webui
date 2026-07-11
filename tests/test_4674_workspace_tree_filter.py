"""Tests for #4674 salvage — workspace loaded-tree file/folder filter.

Phase-1 client-side filter on the workspace Files tab (follow-up to #4673).
These tests guard both the filter's core recursive behavior (via a Node VM)
and the maintainer fix-spec applied during the salvage rebuild:

  * forced-expand folders collapse on the FIRST click (no "dead first click")
  * the filter oninput is debounced (~150ms)
  * the Escape handler only preventDefault when the input has a value
  * new i18n keys exist in every locale block (parity)
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
CSS_PATH = REPO_ROOT / "static" / "style.css"

UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
INDEX_HTML = INDEX_HTML_PATH.read_text(encoding="utf-8")
I18N_JS = I18N_JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8")

NODE = shutil.which("node") or (
    str(Path.home() / ".local" / "bin" / "node")
    if (Path.home() / ".local" / "bin" / "node").exists()
    else None
)


# ── Wiring / presence locks ───────────────────────────────────────────────────


class TestFilterWiring:
    def test_filter_functions_defined(self):
        for fn in (
            "function _filterWorkspaceTreeEntries(",
            "function setWorkspaceTreeFilter(",
            "function clearWorkspaceTreeFilter(",
            "function _workspaceTreeFilterNeedle(",
            "function _workspaceEntryMatchesFilter(",
        ):
            assert fn in UI_JS, f"missing filter function: {fn}"

    def test_search_row_markup_present(self):
        assert 'class="workspace-search-row"' in INDEX_HTML
        assert 'id="workspaceFilterInput"' in INDEX_HTML
        assert 'id="workspaceFilterClearBtn"' in INDEX_HTML
        assert 'oninput="setWorkspaceTreeFilter(this.value)"' in INDEX_HTML

    def test_search_row_css_present(self):
        assert ".workspace-search-row{" in CSS
        assert ".workspace-search-input{" in CSS
        # search row hidden on the artifacts + todos tabs
        assert '.rightpanel[data-active-tab="artifacts"] .workspace-search-row' in CSS
        assert '.rightpanel[data-active-tab="todos"] .workspace-search-row' in CSS

    def test_i18n_keys_present_in_all_locales(self):
        # Every en key must exist in every locale block (parity). Both new keys
        # must appear exactly as often as an existing universal workspace key.
        n_blocks = I18N_JS.count("workspace_empty_dir:")
        assert n_blocks >= 13, f"expected >=13 locale blocks, found {n_blocks}"
        for key in ("workspace_filter_placeholder:", "workspace_filter_no_matches:"):
            count = I18N_JS.count(key)
            assert count == n_blocks, (
                f"{key} appears {count}x but workspace_empty_dir appears {n_blocks}x; "
                "the strict locale-parity suite fails on en-only keys"
            )


# ── Maintainer fix-spec locks ─────────────────────────────────────────────────


class TestFixSpec:
    def test_forced_expand_first_click_collapses(self):
        """A filter-forced-open folder must be treated as expanded so the first
        click collapses it (the #4674 'dead first click' bug)."""
        assert "if(S._expandedDirs.has(item.path)||forcedExpanded){" in UI_JS, (
            "directory click handler must treat forcedExpanded as expanded so the "
            "first click collapses a filter-forced-open folder"
        )

    def test_oninput_is_debounced(self):
        """setWorkspaceTreeFilter must debounce the re-render (~150ms) so a
        future backend search does not fire per keystroke."""
        m = re.search(
            r"function setWorkspaceTreeFilter\(value\)\{(.*?)\n\}",
            UI_JS,
            re.DOTALL,
        )
        assert m, "setWorkspaceTreeFilter body not found"
        body = m.group(1)
        assert "setTimeout" in body, "setWorkspaceTreeFilter must debounce via setTimeout"
        delay_m = re.search(r"setTimeout\([^,]+,\s*(\d+)\s*\)", body)
        assert delay_m, "debounce setTimeout with numeric delay not found"
        delay = int(delay_m.group(1))
        assert 100 <= delay <= 300, f"debounce should be ~150ms; got {delay}ms"

    def test_escape_scoped_to_populated_input(self):
        """The Escape handler must only preventDefault when the input has a
        value, so a global panel-close Escape is not masked on an empty input."""
        m = re.search(
            r'id="workspaceFilterInput".*?onkeydown="([^"]*)"',
            INDEX_HTML,
            re.DOTALL,
        )
        assert m, "workspaceFilterInput onkeydown not found"
        handler = m.group(1)
        assert "Escape" in handler
        assert "this.value" in handler, (
            "Escape handler must gate on this.value so an empty-input Escape "
            "falls through to any global panel-close handler"
        )
        assert "preventDefault" in handler


# ── #5911 gate fix-spec locks (state-management bugs) ─────────────────────────


PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
WORKSPACE_JS = WORKSPACE_JS_PATH.read_text(encoding="utf-8")


class TestGateFix5911:
    # Finding 1 — pending filter debounce cancelled/invalidated on switch.
    def test_switch_cancels_pending_filter_timer_before_skeleton(self):
        """A workspace/profile switch must cancel any queued filter re-render
        BEFORE showing the loading skeleton, so the debounce can't repaint the
        previous workspace's interactive tree over the skeleton."""
        assert "function _cancelWorkspaceFilterRenderTimer(" in UI_JS
        # The cancel must run at the switch point, before the skeleton render.
        m = re.search(
            r"_cancelWorkspaceFilterRenderTimer\(\);\s*\n\s*if \(_workspaceVisibleAtStart"
            r".*?showWorkspaceTreeSkeleton",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "switch path must call _cancelWorkspaceFilterRenderTimer() immediately "
            "before showWorkspaceTreeSkeleton()"
        )

    def test_filter_timer_callback_is_tree_gen_guarded(self):
        """The debounce callback must capture the tree generation when scheduled
        and no-op if a switch bumped it while the timer was pending."""
        m = re.search(
            r"function setWorkspaceTreeFilter\(value\)\{(.*?)\n\}",
            UI_JS,
            re.DOTALL,
        )
        assert m, "setWorkspaceTreeFilter body not found"
        body = m.group(1)
        assert "_wsTreeGenSnapshot()" in body, (
            "setWorkspaceTreeFilter must capture the tree generation when scheduling"
        )
        # The comparison must live INSIDE the setTimeout callback (after scheduling)
        # and short-circuit the render.
        cb = re.search(r"setTimeout\(\(\)=>\{(.*?)\},\s*\d+\)", body, re.DOTALL)
        assert cb, "debounce setTimeout callback not found"
        cb_body = cb.group(1)
        assert "_wsTreeGenSnapshot()!==scheduledGen" in cb_body and "return" in cb_body, (
            "timer callback must no-op when the tree generation changed while pending"
        )

    # Finding 2 — force-opened folder stays collapsed after an explicit click.
    def test_filter_local_collapse_set_declared(self):
        assert "S._filterCollapsedDirs=new Set()" in UI_JS, (
            "must track filter-local collapsed paths in a Set"
        )

    def test_forced_expand_and_children_gated_on_filter_collapse(self):
        """Both forcedExpanded and showFilteredChildren must be suppressed for a
        path in _filterCollapsedDirs, so a collapse click sticks despite the
        filter still matching descendants."""
        assert "!_filterCollapsed&&Array.isArray(item._filteredChildren)" in UI_JS.replace(
            " ", ""
        ) or "!_filterCollapsed&&Array.isArray(item._filteredChildren)" in UI_JS
        # forcedExpanded gated
        assert re.search(
            r"const forcedExpanded=filterActive&&isDirLike&&!_filterCollapsed",
            UI_JS,
        ), "forcedExpanded must be gated on !_filterCollapsed"
        # showFilteredChildren gated
        assert re.search(
            r"const showFilteredChildren=filterActive&&!_filterCollapsed",
            UI_JS,
        ), "showFilteredChildren must be gated on !_filterCollapsed"
        # click handler records the collapse
        assert "S._filterCollapsedDirs.add(item.path)" in UI_JS, (
            "collapse click must record the path in _filterCollapsedDirs"
        )

    def test_filter_collapse_cleared_when_filter_changes(self):
        """Changing or clearing the filter value must reset filter-local
        collapse state."""
        # setWorkspaceTreeFilter clears on value change
        m = re.search(
            r"function setWorkspaceTreeFilter\(value\)\{(.*?)\n\}", UI_JS, re.DOTALL
        )
        assert m and "S._filterCollapsedDirs.clear()" in m.group(1)
        # clearWorkspaceTreeFilter clears too
        m2 = re.search(
            r"function clearWorkspaceTreeFilter\(\)\{(.*?)\n\}", UI_JS, re.DOTALL
        )
        assert m2 and "S._filterCollapsedDirs.clear()" in m2.group(1)

    def test_collapse_behavior_via_node(self):
        """End-to-end: reproduce the render decision. A force-opened folder that
        the user collapsed (path in _filterCollapsedDirs) must resolve to NOT
        force-expanded and NOT showing filtered children; the same folder with
        an empty collapse set stays force-open."""
        js = r"""
function decide(filterActive, item, filterCollapsedDirs){
  const has = (p) => filterCollapsedDirs.has(p);
  const _filterCollapsed = filterActive && has(item.path);
  const forcedExpanded = filterActive && !_filterCollapsed
    && Array.isArray(item._filteredChildren) && !!item._filteredChildren.length;
  const showFilteredChildren = filterActive && !_filterCollapsed
    && Array.isArray(item._filteredChildren) && !!item._filteredChildren.length;
  return {forcedExpanded, showFilteredChildren};
}
const item = {path:'src', _filteredChildren:[{name:'a'},{name:'b'}]};
const open = decide(true, item, new Set());
const collapsed = decide(true, item, new Set(['src']));
console.log(JSON.stringify({open, collapsed}));
"""
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["open"]["forcedExpanded"] is True
        assert out["open"]["showFilteredChildren"] is True
        assert out["collapsed"]["forcedExpanded"] is False
        assert out["collapsed"]["showFilteredChildren"] is False

    # Finding 3 — same-workspace root nav / refresh preserves the filter.
    def test_loaddir_clears_filter_only_on_workspace_identity_change(self):
        """loadDir root/refresh must clear the filter ONLY when the workspace
        path differs from the last-filtered one; a same-workspace load must
        preserve S.workspaceTreeFilter."""
        assert "let _lastFilteredWorkspacePath" in WORKSPACE_JS, (
            "must track the last-filtered workspace path"
        )
        # The clear must be gated on an identity difference.
        assert re.search(
            r"_lastFilteredWorkspacePath!==null && _curWs!==_lastFilteredWorkspacePath",
            WORKSPACE_JS,
        ), "filter clear must be gated on a workspace identity change"

    def test_switch_to_workspace_bumps_tree_gen(self):
        """#5911 gate: a DIRECT switchToWorkspace() must bump the workspace-tree
        generation so a filter-render debounce timer scheduled for the previous
        workspace (which guards on _wsTreeGenSnapshot()) cannot paint the old
        workspace's filtered entries under the new one."""
        start = PANELS_JS.find("async function switchToWorkspace(")
        assert start != -1, "switchToWorkspace not found"
        body = PANELS_JS[start:PANELS_JS.find("\n}", start)]
        assign = body.find("S.session.workspace=path;")
        bump = body.find("bumpWorkspaceTreeGen", assign)
        assert assign != -1 and bump != -1 and assign < bump, (
            "switchToWorkspace must call bumpWorkspaceTreeGen() after changing "
            "S.session.workspace so a stale filter timer is invalidated"
        )

    def test_workspace_identity_gate_via_node(self):
        """Reproduce the loadDir root-load filter-clear decision: same workspace
        preserves the filter; a workspace change clears it."""
        js = r"""
// Mirror the loadDir('.') gate: clear the filter only on identity change.
function rootLoad(state){
  const _curWs = state.session && state.session.workspace || null;
  if(state._lastFilteredWorkspacePath!==null && _curWs!==state._lastFilteredWorkspacePath){
    if(typeof state.workspaceTreeFilter==='string' && state.workspaceTreeFilter){
      state.workspaceTreeFilter='';
    }
  }
  state._lastFilteredWorkspacePath = _curWs;
  return state;
}
// same-workspace refresh: filter preserved
let s1 = {session:{workspace:'/ws/a'}, workspaceTreeFilter:'keep-me', _lastFilteredWorkspacePath:'/ws/a'};
rootLoad(s1);
// workspace change: filter cleared
let s2 = {session:{workspace:'/ws/b'}, workspaceTreeFilter:'keep-me', _lastFilteredWorkspacePath:'/ws/a'};
rootLoad(s2);
console.log(JSON.stringify({same:s1.workspaceTreeFilter, changed:s2.workspaceTreeFilter}));
"""
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["same"] == "keep-me", "same-workspace load must preserve the filter"
        assert out["changed"] == "", "workspace-change load must clear the filter"


# ── Behavioral tests (Node VM) ────────────────────────────────────────────────


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _filter_fn_source() -> str:
    """Extract the three pure helpers the filter behavior depends on."""
    parts = []
    for name in (
        "_workspaceEntryMatchesFilter",
        "_filterWorkspaceTreeEntries",
    ):
        m = re.search(rf"function {name}\(.*?\n\}}", UI_JS, re.DOTALL)
        assert m, f"could not extract {name}"
        parts.append(m.group(0))
    return "\n".join(parts)


def _run_filter(entries, dir_cache, needle):
    src = _filter_fn_source()
    payload = {"entries": entries, "dirCache": dir_cache, "needle": needle}
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
const S = { _dirCache: params.dirCache };
// _visibleWorkspaceEntries is a hidden-file filter in the real app; here we
// pass entries through unchanged so the test isolates the filter logic.
const _visibleWorkspaceEntries = (e) => e || [];
"""
        + src
        + r"""
const out = _filterWorkspaceTreeEntries(params.entries, params.needle);
console.log(JSON.stringify(out));
"""
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestFilterBehavior:
    def test_empty_needle_returns_all(self):
        entries = [{"type": "file", "name": "a.txt", "path": "a.txt"}]
        out = _run_filter(entries, {}, "")
        assert len(out) == 1

    def test_matches_by_name(self):
        entries = [
            {"type": "file", "name": "readme.md", "path": "readme.md"},
            {"type": "file", "name": "main.py", "path": "main.py"},
        ]
        out = _run_filter(entries, {}, "readme")
        assert [e["name"] for e in out] == ["readme.md"]

    def test_matches_by_path(self):
        entries = [{"type": "file", "name": "x.py", "path": "src/deep/x.py"}]
        out = _run_filter(entries, {}, "deep")
        assert len(out) == 1

    def test_preserves_ancestor_and_surfaces_descendant(self):
        """A collapsed directory whose CACHED descendant matches must be kept
        and its matching child surfaced via _filteredChildren."""
        entries = [{"type": "dir", "name": "src", "path": "src"}]
        dir_cache = {
            "src": [
                {"type": "file", "name": "match.js", "path": "src/match.js"},
                {"type": "file", "name": "other.js", "path": "src/other.js"},
            ]
        }
        out = _run_filter(entries, dir_cache, "match")
        assert len(out) == 1
        assert out[0]["name"] == "src"
        assert "_filteredChildren" in out[0]
        assert [c["name"] for c in out[0]["_filteredChildren"]] == ["match.js"]

    def test_non_matching_dir_dropped(self):
        entries = [{"type": "dir", "name": "docs", "path": "docs"}]
        dir_cache = {"docs": [{"type": "file", "name": "a.md", "path": "docs/a.md"}]}
        out = _run_filter(entries, dir_cache, "zzz")
        assert out == []

    def test_external_symlink_not_recursed(self):
        """An external symlink is display-only and must not be treated as a
        recursable directory."""
        entries = [
            {
                "type": "symlink",
                "name": "ext",
                "path": "ext",
                "is_dir": True,
                "target_outside_workspace": True,
            }
        ]
        out = _run_filter(entries, {"ext": [{"type": "file", "name": "secret", "path": "ext/secret"}]}, "secret")
        assert out == []


def test_filter_timer_pins_workspace_identity():
    # #5911 gate (round 3): the debounced filter render must also pin the
    # workspace it was scheduled for — not every workspace-changing path bumps
    # _wsTreeGen (plain session load / new-chat nav), so the callback must no-op
    # when S.session.workspace changed while the debounce was pending.
    ui = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "const scheduledWs=(S.session&&S.session.workspace)||'';" in ui, (
        "filter render must capture the scheduled workspace identity"
    )
    assert "if(((S.session&&S.session.workspace)||'')!==scheduledWs) return;" in ui, (
        "filter render timer callback must no-op when the workspace changed while pending"
    )
