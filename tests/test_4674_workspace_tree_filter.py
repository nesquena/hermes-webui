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
