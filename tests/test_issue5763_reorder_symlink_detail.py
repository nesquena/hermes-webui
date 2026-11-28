"""Round-8 item 1 (re-gate 2026-10-12, head c67105b8): after a drag-reorder,
a native project registered through a symlink becomes unclickable.

Reorder renders the alias path; the follow-up ``loadWorkspaceList()`` refresh
replaces ``_workspaceList`` with the host-resolved path. The still-visible row
then calls ``openWorkspaceDetail(aliasObj)`` and the exact path+id+kind lookup
misses (path differs, id/kind match), so no detail panel opens.

Fix: when the clicked row carries a ``project_id``, the id is the authoritative
native identity and path equality may be relaxed; rows without a project id
still require exact path equality, and a stale/absent id stays rejected.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _extract_fn(src: str, header: str) -> str:
    i = src.index(header)
    j = src.index("\n}\n", i)
    return src[i:j + 2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_open_detail_accepts_alias_row_after_cache_refresh():
    """Alias-path row with a project_id opens the resolved-path row's detail."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fns = _extract_fn(src, "function openWorkspaceDetail(pathOrWs, el){")
    script = textwrap.dedent("""
        const assert = require('assert');
        let _workspaceList = [], _workspaceUiGen = 0, _workspacePreFormDetail = null;
        let rendered = null, closedMobile = 0;
        const _renderWorkspaceDetail = (ws) => { rendered = ws; };
        const _closeMobileSidebarAfterPanelSelection = () => { closedMobile++; };
        const rows = [];
        const document = {
          querySelectorAll: (sel) => (sel === '.ws-row' ? rows : []),
        };
        __FNS__

        // Refreshed cache holds the host-resolved spelling of the native project.
        const RESOLVED = {path: '/real/proj', name: 'Proj', project_id: 'N1', workspace_kind: ''};
        _workspaceList = [RESOLVED];

        // The visible row still carries the alias spelling from before the refresh.
        const ALIAS = {path: '/alias/proj', name: 'Proj', project_id: 'N1', workspace_kind: ''};
        openWorkspaceDetail(ALIAS);
        assert.strictEqual(rendered, RESOLVED,
          'alias row with a project_id must open the resolved row after the cache refresh');
        assert.strictEqual(closedMobile, 1);

        // Controls: rows without a project id still require exact path equality.
        rendered = null;
        openWorkspaceDetail({path: '/alias/proj', name: 'Plain', project_id: '', workspace_kind: ''});
        assert.strictEqual(rendered, null, 'path-only row must not match a different path');

        // Control: a stale native id (row removed from the refreshed list) stays rejected.
        rendered = null;
        openWorkspaceDetail({path: '/alias/proj', name: 'Gone', project_id: 'N9', workspace_kind: ''});
        assert.strictEqual(rendered, null, 'stale project_id must not open any row');

        // Control: remote identity (kind set, no id) still requires path equality.
        rendered = null;
        openWorkspaceDetail({path: '/alias/proj', name: 'Remote', project_id: '', workspace_kind: 'remote'});
        assert.strictEqual(rendered, null, 'remote row must not match on kind alone');

        // Positive control: exact identity still opens.
        rendered = null;
        openWorkspaceDetail(RESOLVED);
        assert.strictEqual(rendered, RESOLVED);
    """).replace("__FNS__", fns)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout
