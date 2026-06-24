"""Regression tests for #4757 — macOS workspace drag-drop never lands.

WebKit strips custom MIME types from DataTransfer during dragover/drop, so
_isWorkspaceTreeMoveDrag must accept text/plain when the module-scoped active-drag
flag is set. The node-driver extraction pattern runs all logic in Node.js without
a browser.
"""
import os
import subprocess
import tempfile

UI_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'ui.js')


def _extract_drag_functions():
    """Extract the six drag-drop functions from ui.js."""
    src = open(UI_JS, encoding='utf-8').read()

    # Extract module-scoped let declarations
    lines = src.split('\n')
    let_lines = []
    for line in lines:
        s = line.strip()
        if s in ('let _wsActiveDragPath=null;', 'let _wsActiveDragType=null;'):
            let_lines.append(s)

    def extract_function(name):
        start = src.find(f'function {name}(')
        if start < 0:
            raise AssertionError(f'{name} not found in ui.js')
        i = src.find('{', start)
        depth = 1
        i += 1
        while i < len(src) and depth:
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
            i += 1
        return src[start:i]

    fns = '\n'.join(
        extract_function(name)
        for name in (
            '_setWsDragData',
            '_isWorkspaceTreeMoveDrag',
            '_wsDragSrcPath',
            '_wsDragSrcType',
        )
    )
    return '\n'.join(let_lines), fns


def _run_node(test_js):
    """Run a Node.js snippet that includes the extracted functions, return stdout."""
    lets, fns = _extract_drag_functions()
    harness = """\
// Fake DataTransfer
class FakeDataTransfer {
  constructor(typesArr, dataMap) {
    this._map = dataMap || {};
    this.types = typesArr || Object.keys(this._map);
  }
  getData(mime) { return this._map[mime] || ''; }
  setData(mime, val) { this._map[mime] = val; if(!this.types.includes(mime)) this.types.push(mime); }
}
"""
    js_code = harness + '\n' + lets + '\n' + fns + '\n' + test_js

    tf = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8')
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ['node', tf.name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f'node error: {result.stderr}')
        return result.stdout.strip()
    finally:
        os.unlink(tf.name)


class TestMacosDragDropMime:
    """Module-scoped active-drag flags allow workspace drag-drop on macOS."""

    def test_macos_stripped_drag_accepted(self):
        """After _setWsDragData, gate accepts text/plain-only (WebKit strips custom MIME)."""
        js = """\
const item = {path: 'docs/readme.md', type: 'file'};
const startDt = new FakeDataTransfer([], {});
startDt.setData = function(mime, val){ this._map[mime]=val; if(!this.types.includes(mime)) this.types.push(mime); };
_setWsDragData({dataTransfer: startDt}, item);

// Simulate macOS: only text/plain survives into dragover/drop
const dropDt = new FakeDataTransfer(['text/plain'], {'text/plain': item.path});
const accepted = _isWorkspaceTreeMoveDrag({dataTransfer: dropDt});
process.stdout.write(String(accepted));
"""
        assert _run_node(js) == 'true', \
            'macOS-stripped drag must be accepted when active-drag flag is set'

    def test_normal_drag_accepted(self):
        """Custom MIME present — gate returns true without needing the flag."""
        js = """\
const dropDt = new FakeDataTransfer(
  ['application/ws-path', 'text/plain'],
  {'application/ws-path': 'notes.txt', 'text/plain': 'notes.txt'}
);
process.stdout.write(String(_isWorkspaceTreeMoveDrag({dataTransfer: dropDt})));
"""
        assert _run_node(js) == 'true', \
            'normal drag (custom MIME present) must be accepted'

    def test_foreign_text_drag_rejected(self):
        """Foreign text (text/plain only, no active-drag flag) must be rejected."""
        js = """\
// Do NOT call _setWsDragData — flag stays null
const dropDt = new FakeDataTransfer(['text/plain'], {'text/plain': 'hello world'});
process.stdout.write(String(_isWorkspaceTreeMoveDrag({dataTransfer: dropDt})));
"""
        assert _run_node(js) == 'false', \
            'foreign text drag must be rejected when no active-drag flag is set'

    def test_files_drag_rejected_even_with_active_flag(self):
        """Files drop must be rejected regardless of active-drag state."""
        js = """\
const item = {path: 'img.png', type: 'file'};
const startDt = new FakeDataTransfer([], {});
startDt.setData = function(mime, val){ this._map[mime]=val; if(!this.types.includes(mime)) this.types.push(mime); };
_setWsDragData({dataTransfer: startDt}, item);

const dropDt = new FakeDataTransfer(['Files', 'text/plain'], {});
process.stdout.write(String(_isWorkspaceTreeMoveDrag({dataTransfer: dropDt})));
"""
        assert _run_node(js) == 'false', \
            'Files drop must be rejected even when active-drag flag is set'

    def test_resolver_returns_custom_mime_when_present(self):
        """_wsDragSrcPath returns custom MIME value when available."""
        js = """\
const dropDt = new FakeDataTransfer(
  ['application/ws-path', 'text/plain'],
  {'application/ws-path': 'src/index.js', 'text/plain': 'fallback'}
);
process.stdout.write(_wsDragSrcPath({dataTransfer: dropDt}));
"""
        assert _run_node(js) == 'src/index.js', \
            '_wsDragSrcPath must prefer custom MIME over text/plain'

    def test_resolver_falls_back_to_active_drag_path(self):
        """_wsDragSrcPath falls back to active-drag flag when custom MIME stripped."""
        js = """\
const item = {path: 'my file.txt', type: 'file'};
const startDt = new FakeDataTransfer([], {});
startDt.setData = function(mime, val){ this._map[mime]=val; if(!this.types.includes(mime)) this.types.push(mime); };
_setWsDragData({dataTransfer: startDt}, item);

// Custom MIME stripped; text/plain has the path too (as set by _setWsDragData)
const dropDt = new FakeDataTransfer(['text/plain'], {'text/plain': item.path});
// Override getData so custom MIME returns empty
dropDt.getData = function(mime){ if(mime==='application/ws-path') return ''; return this._map[mime]||''; };
process.stdout.write(_wsDragSrcPath({dataTransfer: dropDt}));
"""
        assert _run_node(js) == 'my file.txt', \
            '_wsDragSrcPath must fall back to active-drag flag (supports paths with spaces)'

    def test_resolver_type_returns_custom_mime_type(self):
        """_wsDragSrcType returns custom type when available."""
        js = """\
const dropDt = new FakeDataTransfer(
  ['application/ws-type'],
  {'application/ws-type': 'dir'}
);
process.stdout.write(_wsDragSrcType({dataTransfer: dropDt}));
"""
        assert _run_node(js) == 'dir', \
            '_wsDragSrcType must return custom MIME type value'

    def test_resolver_type_falls_back_to_active_drag_type(self):
        """_wsDragSrcType falls back to active-drag flag when custom MIME stripped."""
        js = """\
const item = {path: 'projects/', type: 'dir'};
const startDt = new FakeDataTransfer([], {});
startDt.setData = function(mime, val){ this._map[mime]=val; if(!this.types.includes(mime)) this.types.push(mime); };
_setWsDragData({dataTransfer: startDt}, item);

const dropDt = new FakeDataTransfer(['text/plain'], {'text/plain': item.path});
dropDt.getData = function(mime){ if(mime==='application/ws-type') return ''; return this._map[mime]||''; };
process.stdout.write(_wsDragSrcType({dataTransfer: dropDt}));
"""
        assert _run_node(js) == 'dir', \
            '_wsDragSrcType must fall back to active-drag type flag'

    def test_ondragend_equivalent_clears_state(self):
        """Clearing flags causes text/plain-only drag to be rejected (ondragend effect)."""
        js = """\
const item = {path: 'tmp.txt', type: 'file'};
const startDt = new FakeDataTransfer([], {});
startDt.setData = function(mime, val){ this._map[mime]=val; if(!this.types.includes(mime)) this.types.push(mime); };
_setWsDragData({dataTransfer: startDt}, item);

// Simulate ondragend clearing flags
_wsActiveDragPath = null;
_wsActiveDragType = null;

const dropDt = new FakeDataTransfer(['text/plain'], {'text/plain': item.path});
process.stdout.write(String(_isWorkspaceTreeMoveDrag({dataTransfer: dropDt})));
"""
        assert _run_node(js) == 'false', \
            'after ondragend clears flags, text/plain-only drag must be rejected'
