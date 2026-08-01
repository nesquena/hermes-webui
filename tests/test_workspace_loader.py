"""Browserless JS loader tests for workspace directory pagination (#6645).

Runs a minimal Node.js harness that stubs the browser globals (fetch, DOM,
S, api, renderFileTree, etc.) and exercises _loadMoreDir's session/treeGen
guards and dedup logic without a real browser.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKSPACE_JS = REPO / "static" / "workspace.js"


def _node_available():
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_append_by_cursor(tmp_path):
    """Pages append under session and render-generation guards without duplicates."""

    # Write harness to a temp file to avoid inline-eval scope conflicts.
    harness_path = tmp_path / "harness.js"
    workspace_js_path = str(WORKSPACE_JS).replace("\\", "/")

    harness = textwrap.dedent(f"""
        // ── DOM / browser stubs ────────────────────────────────────────────
        const _fakeBox = {{
          children: [], innerHTML: '', scrollTop: 0, dataset:{{}}, style:{{}},
          classList:{{ add(){{}}, remove(){{}}, contains(){{ return false; }}, toggle(){{}} }},
          appendChild(el){{ this.children.push(el); }},
          removeChild(){{}},
          querySelector(sel){{ return null; }},
          querySelectorAll(sel){{ return []; }},
          addEventListener(){{}},
          removeEventListener(){{}},
          contains(){{ return false; }},
          closest(){{ return null; }},
        }};
        const _fakeEl = {{
          className:'', style:{{}}, textContent:'', dataset:{{}},
          innerHTML:'', scrollTop:0, children:[],
          setAttribute(){{}}, getAttribute(){{ return null; }},
          classList:{{ add(){{}}, remove(){{}}, contains(){{ return false; }}, toggle(){{}} }},
          appendChild(){{}}, removeChild(){{}}, remove(){{}},
          addEventListener(){{}}, removeEventListener(){{}},
          querySelector(){{ return null; }},
          querySelectorAll(){{ return []; }},
        }};
        global.document = {{
          createElement(tag){{ return Object.create(_fakeEl); }},
          baseURI: 'http://localhost/',
          querySelector(){{ return null; }},
          querySelectorAll(){{ return []; }},
          addEventListener(){{}},
          body: Object.create(_fakeEl),
          head: Object.create(_fakeEl),
        }};
        global.location = {{ href: 'http://localhost/' }};
        global.window = global;
        class _FakeURL {{
          constructor(rel, base){{ this.href = (base || '') + rel; }}
          toString(){{ return this.href; }}
        }}
        global.URL = _FakeURL;
        global.localStorage = {{ getItem(){{ return null; }}, setItem(){{}} }};

        // ── app globals stubs ─────────────────────────────────────────────
        const S = {{
          session: {{ session_id: 'test-sid', workspace: '/ws' }},
          entries: [],
          currentDir: '.',
          _dirCache: {{}},
          _dirCursor: null,
          _dirHasMore: false,
          _expandedDirs: new Set(),
        }};
        global.S = S;

        global.renderBreadcrumb = function(){{}};
        global.renderFileTree = function(){{}};
        global.showToast = function(){{}};
        global.t = function(key){{ return key; }};
        global.$ = function(id){{ return id === 'fileTree' ? _fakeBox : null; }};
        global.clearPreview = function(){{}};
        global._refreshGitBadge = function(){{}};
        global._workspaceRouteForPath = function(){{ return null; }};
        global._workspaceEscapeGrantForPath = function(){{ return null; }};
        global._clearWorkspaceEscapeGrant = function(){{}};
        global.syncWorkspaceDisplays = function(){{}};
        global.syncTerminalButton = function(){{}};
        global.refreshOpenPreviewIfMutated = async function(){{}};
        global._restoreExpandedDirs = function(){{}};
        global._saveExpandedDirs = function(){{}};
        global.renderSessionArtifacts = function(){{}};
        global.showConfirmDialog = function(){{ return Promise.resolve(false); }};
        global.bumpWorkspaceTreeGen = function(){{}};  // placeholder, overwritten by workspace.js
        global._workspacePathIsReadOnly = function(){{ return false; }};
        global.uploadOsDropToWorkspace = async function(){{}};

        // ── load workspace.js (evaluates in this file's module scope) ─────
        const fs = require('fs');
        const vm = require('vm');
        // workspace.js declares its own `async function api(...)` which overrides
        // global.api.  Load it first, then overwrite with the stub below.
        const _wsCode = fs.readFileSync('{workspace_js_path}', 'utf8');
        vm.runInThisContext(_wsCode, {{filename: '{workspace_js_path}'}});

        // ── fetch stub (must come AFTER vm.runInThisContext to overwrite workspace api) ──
        const _apiResponses = [];
        // eslint-disable-next-line no-global-assign
        api = async function(url, opts){{
          if(!_apiResponses.length) throw new Error('no api response queued for: ' + url);
          return _apiResponses.shift();
        }};

        // ── tests ─────────────────────────────────────────────────────────
        const assert = require('assert');

        async function run(){{
          // ── test 1: append second page, no duplicates ──────────────────
          const page1 = Array.from({{length: 200}}, (_, i) => {{
            const n = 'f' + String(i).padStart(4,'0') + '.txt';
            return {{name: n, path: n, type: 'file'}};
          }});
          const page2 = [{{name: 'f0200.txt', path: 'f0200.txt', type: 'file'}}];

          S.entries = page1.slice();
          S._dirHasMore = true;
          S._dirCursor = 'valid-cursor-value';
          S.currentDir = '.';
          S.session = {{session_id: 'test-sid', workspace: '/ws'}};
          bumpWorkspaceTreeGen();  // advance to a known gen

          let renderCalled = 0;
          global.renderFileTree = function(){{ renderCalled++; }};

          _apiResponses.push({{entries: page2, has_more: false, cursor: null}});
          await _loadMoreDir();

          assert.strictEqual(S.entries.length, 201, 'append: expected 201 entries');
          assert.strictEqual(S._dirHasMore, false, 'append: has_more should be false after last page');
          assert.strictEqual(S._dirCursor, null, 'append: cursor should be null after last page');
          assert(renderCalled > 0, 'append: renderFileTree should have been called');

          const allNames = S.entries.map(function(e){{ return e.name; }});
          const unique = new Set(allNames);
          assert.strictEqual(unique.size, 201, 'append: no duplicates expected');

          // ── test 2: session guard — mismatched session_id → no append ──
          S.entries = page1.slice();
          S._dirHasMore = true;
          S._dirCursor = 'cursor2';
          S.session = {{session_id: 'sid-before', workspace: '/ws'}};
          const entriesBefore = S.entries.length;

          _apiResponses.push({{entries: page2, has_more: false, cursor: null}});
          // _loadMoreDir captures sessionId synchronously then awaits api().
          // Switch the session before the microtask resolves.
          const loadPromise = _loadMoreDir();
          S.session = {{session_id: 'sid-after', workspace: '/ws'}};
          await loadPromise;

          assert.strictEqual(S.entries.length, entriesBefore,
            'session guard: entries must not change after session switch');

          // ── test 3: treeGen guard — bumped gen → no append ─────────────
          S.entries = page1.slice();
          S._dirHasMore = true;
          S._dirCursor = 'cursor3';
          S.session = {{session_id: 'test-sid', workspace: '/ws'}};

          _apiResponses.push({{entries: page2, has_more: false, cursor: null}});
          // _loadMoreDir captures treeGen; bump it before the microtask resolves.
          const loadPromise2 = _loadMoreDir();
          bumpWorkspaceTreeGen();  // increments the internal _wsTreeGen
          await loadPromise2;

          assert.strictEqual(S.entries.length, page1.length,
            'treeGen guard: entries must not change after gen bump');

          console.log('PASS');
        }}

        run().then(function(){{ process.exit(0); }}).catch(function(e){{ console.error('FAIL:', e.message, e.stack); process.exit(1); }});
    """)

    harness_path.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        ["node", str(harness_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=30,
    )

    if result.returncode != 0:
        pytest.fail(
            f"JS loader harness failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr[:2000]}"
        )

    assert "PASS" in result.stdout, (
        f"JS loader harness did not print PASS:\n{result.stdout}\n{result.stderr[:500]}"
    )
