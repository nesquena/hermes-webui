"""Regression: identical replies must not share a mermaid render id.

Runs the real _uniqueMermaidRenderId() + renderMermaidBlocks() from static/ui.js.

Rendered markdown is cached by content, so two assistant replies with the same
```mermaid block are emitted with the same ``data-mermaid-id``. mermaid.render()
then fails for every block after the first (the id is already taken in the
document) and the diagram falls back to a code block. renderMermaidBlocks()
must hand each block a unique id before rendering.

The test extracts the real renderMermaidBlocks() from static/ui.js and runs it
in node against a minimal fake DOM and a fake mermaid that, like mermaid 10,
rejects an id that already exists in the document.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).parent.parent
UI_JS = ROOT / "static" / "ui.js"


def _extract_render_mermaid_blocks() -> str:
    src = UI_JS.read_text(encoding="utf-8")
    start = src.index("function _uniqueMermaidRenderId(block){")
    end = src.index("\nlet _katexLoading", start)
    return src[start:end]


def test_render_mermaid_blocks_assigns_unique_ids_to_duplicate_blocks():
    """Same render pass, a later reply after the first rendered, and an untouched unique id."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for this runtime test")
    fn = _extract_render_mermaid_blocks()
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const all = [];               // every element "in the document"
        function makeBlock(id, code) {
          const el = {
            id: '', dataset: {mermaidId: id}, textContent: code, innerHTML: '',
            classList: {
              set: new Set(['mermaid-block']),
              add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); }, contains(c) { return this.set.has(c); },
            },
            querySelector() { return null; },
          };
          all.push(el);
          return el;
        }
        global.document = {
          getElementById(id) { return all.find(e => e.id === id) || null; },
          querySelectorAll(sel) {
            const m = /^\\[data-mermaid-id="(.*)"\\]$/.exec(sel);
            if (m) return all.filter(e => e.dataset && e.dataset.mermaidId === m[1]);
            return [];
          },
        };
        global.esc = s => String(s);
        global._mountMermaidViewer = () => {};
        global._mermaidReady = true;
        global._mermaidLoading = false;
        global.mermaid = {
          async render(id, code) {
            if (all.some(e => e.id === id)) throw new Error('duplicate id ' + id);
            all.push({id, dataset: {}});   // the rendered <svg id=...> now occupies the id
            return {svg: '<svg id="' + id + '"></svg>'};
          },
        };
        const code = 'flowchart TD\\n  A --> B';
        %FN%
        (async () => {
          // 1) both identical blocks in the same render pass
          const a = makeBlock('mermaid-same', code);
          const b = makeBlock('mermaid-same', code);
          renderMermaidBlocks({querySelectorAll: () => [a, b]});
          await new Promise(r => setTimeout(r, 30));
          assert.ok(a.classList.contains('mermaid-rendered'), 'first block renders');
          assert.ok(b.classList.contains('mermaid-rendered'), 'second identical block renders too');
          assert.notStrictEqual(a.dataset.mermaidId, b.dataset.mermaidId, 'ids are unique after rendering');
          // 2) a later reply with the same cached id arrives after the first diagram already rendered
          const c = makeBlock(a.dataset.mermaidId, code);
          renderMermaidBlocks({querySelectorAll: () => [c]});
          await new Promise(r => setTimeout(r, 30));
          assert.ok(c.classList.contains('mermaid-rendered'), 'a later duplicate reply renders');
          assert.notStrictEqual(c.dataset.mermaidId, a.dataset.mermaidId, 'later duplicate gets its own id');
          // 3) a unique id is left untouched
          const d = makeBlock('mermaid-unique', code);
          renderMermaidBlocks({querySelectorAll: () => [d]});
          await new Promise(r => setTimeout(r, 30));
          assert.strictEqual(d.dataset.mermaidId, 'mermaid-unique', 'unique ids are not rewritten');
          console.log('ok');
        })().catch(e => { console.error(e); process.exit(1); });
        """
    ).replace("%FN%", fn)
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "ok" in result.stdout
