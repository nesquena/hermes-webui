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
# Capture real subprocess primitives at import time, before any fixture patches them.
_real_subprocess_popen = subprocess.Popen
_real_subprocess_run = subprocess.run


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Pure unit tests; no running server needed."""


@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """No-op override: these unit tests create no server sessions to clean up."""
    yield []


@pytest.fixture(autouse=True)
def _block_popen(monkeypatch):
    """Block accidental server spawns.

    This file legitimately needs subprocess to run the Node.js harness, so the
    fixture restores both Popen and run to their real implementations rather than
    raising.  The guard still fires for any file that does NOT restore them and
    ends up calling Popen unexpectedly.
    """
    monkeypatch.setattr(subprocess, "Popen", _real_subprocess_popen)
    monkeypatch.setattr(subprocess, "run", _real_subprocess_run)


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
        const _apiUrls = [];
        // eslint-disable-next-line no-global-assign
        api = async function(url, opts){{
          _apiUrls.push(url);
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
          assert(_apiUrls[0].includes('/api/list?session_id=test-sid&path=.&cursor='),
            'continuation must use the shared workspace route helper');

          // Escape-directory continuation uses the authorized route, base URI, and token.
          S._escapeGrants = {{escape:{{sessionId:'test-sid',path:'escape',token:'grant',expiresAt:Date.now()+60000}}}};
          const escapeRoute = _workspaceRouteForPath('escape/sub', 'list', {{cursor:'next'}});
          assert(escapeRoute.includes('http://localhost/api/escape/list?'),
            'escape continuation must use the escape route');
          assert(escapeRoute.includes('token=grant') && escapeRoute.includes('cursor=next'),
            'escape continuation must retain authorization and cursor');
          _apiResponses.push({{entries:[{{name:'file.txt',path:'escape/file.txt'}}],has_more:false,cursor:null}});
          assert.strictEqual(await _workspacePathExists('escape/file.txt'), true,
            'artifact existence must use the same escape route helper');
          assert(_apiUrls[_apiUrls.length-1].includes('/api/escape/list?'),
            'artifact existence must retain the escape route');

          // The fetch-all consumer has no second page-count truncation boundary.
          const manyPages = Array.from({{length:501}}, (_, i) =>
            ({{entries:[{{name:'p'+i,path:'p'+i}}],has_more:i<500,cursor:i<500?'c'+i:null}}));
          _apiResponses.push(...manyPages);
          const allPages = await _fetchAllPages('/api/list?session_id=test-sid&path=expanded');
          assert.strictEqual(allPages.length, 501, 'fetch-all must follow every cursor page');

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

          // ── test 4: navigation ownership — stale first page cannot repaint ──
          S.session = {{session_id:'test-sid',workspace:'/ws'}};
          S.currentDir = '.';
          S.entries = [];
          const rootDeferred = new Promise(resolve => {{ global._resolveRoot = resolve; }});
          const subDeferred = new Promise(resolve => {{ global._resolveSub = resolve; }});
          _apiResponses.push(rootDeferred, subDeferred);
          const rootLoad = loadDir('.');
          const subLoad = loadDir('sub');
          global._resolveRoot({{entries:[{{name:'root.txt',path:'root.txt'}}],has_more:false,cursor:null}});
          global._resolveSub({{entries:[{{name:'sub.txt',path:'sub/sub.txt'}}],has_more:false,cursor:null}});
          await Promise.all([rootLoad, subLoad]);
          assert.strictEqual(S.currentDir, 'sub', 'latest navigation should own currentDir');
          assert.deepStrictEqual(S.entries.map(e => e.name), ['sub.txt'],
            'stale first-page response must not repaint the newer directory');

          // A same-path refresh owns a new cursor, so the old continuation is rejected.
          S.currentDir = 'same';
          S.entries = [{{name:'fresh.txt',path:'fresh.txt'}}];
          S._dirCursor = 'old-same-cursor';
          S._dirHasMore = true;
          const oldPage = new Promise(resolve => {{ global._resolveOldPage = resolve; }});
          _apiResponses.push(oldPage);
          const oldContinuation = _loadMoreDir();
          S._dirCursor = 'new-same-cursor';
          global._resolveOldPage({{entries:[{{name:'stale.txt',path:'stale.txt'}}],has_more:false,cursor:null}});
          await oldContinuation;
          assert.deepStrictEqual(S.entries.map(e => e.name), ['fresh.txt'],
            'same-path refresh must reject the old continuation response');

          // ── test 5: stale continuation 404 cannot clear newer cursor ──
          S.currentDir = 'sub';
          S._dirCursor = 'old-cursor';
          S._dirHasMore = true;
          const stale404 = Promise.reject({{status:404}});
          _apiResponses.push(stale404);
          const staleLoad = _loadMoreDir();
          S.currentDir = 'newer';
          S._dirCursor = 'new-cursor';
          await staleLoad;
          assert.strictEqual(S._dirCursor, 'new-cursor',
            'stale continuation must not clear a newer cursor');

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
