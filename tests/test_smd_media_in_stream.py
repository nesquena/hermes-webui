"""Focused regression coverage for MEDIA tokens in the streaming text path."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_js_function(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    if not match:
        raise ValueError(f"Could not find JS function {name}")
    start = match.start()
    brace = src.index("{", match.end())
    depth = 1
    pos = brace + 1
    while pos < len(src) and depth:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    return src[start:pos]


def _run_media_aware_chunks(chunks: list[str]) -> dict:
    """Drive the real MEDIA chunk scanner with a minimal DOM shim."""
    helpers = "\n".join(
        [
            _extract_js_function(UI_JS, "_mediaTokenParts"),
            _extract_js_function(MESSAGES_JS, "_smdMediaPrefixTail"),
            _extract_js_function(MESSAGES_JS, "_smdMediaWriteText"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSet"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailEntryChunk"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSameOwner"),
            _extract_js_function(MESSAGES_JS, "_smdMediaRefHasReliableBoundary"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTokenParts"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailFlushEntry"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailFlush"),
            _extract_js_function(MESSAGES_JS, "_smdMediaAwareAddText"),
            _extract_js_function(MESSAGES_JS, "_smdAppendMediaNode"),
            _extract_js_function(MESSAGES_JS, "_smdScheduleMediaPostProcess"),
        ]
    )
    script = f"""
const _MEDIA_TAIL_MAX=4096;
const _SMD_MEDIA_PREFIX='MEDIA:';
const _SMD_MEDIA_TAIL=new WeakMap();
function esc(value){{return String(value??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));}}
function _inlineMediaHtmlForRef(ref){{return `<span class=\"media-node\" data-ref=\"${{esc(ref)}}\"></span>`;}}
class FakeNode{{
  constructor(type,tag='',text=''){{this.nodeType=type;this.tagName=tag;this.children=[];this.parentNode=null;this.attributes={{}};this.data=text;}}
  get childNodes(){{return this.children;}}
  get firstChild(){{return this.children[0]||null;}}
  appendChild(child){{
    if(child.nodeType===11){{while(child.firstChild)this.appendChild(child.firstChild);return child;}}
    if(child.parentNode){{const i=child.parentNode.children.indexOf(child);if(i>=0)child.parentNode.children.splice(i,1);}}
    child.parentNode=this;this.children.push(child);return child;
  }}
  setAttribute(name,value){{this.attributes[name]=String(value);}}
  get textContent(){{return this.nodeType===3?this.data:this.children.map(c=>c.textContent).join('');}}
  get outerHTML(){{
    if(this.nodeType===3)return esc(this.data);
    if(this.nodeType===11)return this.children.map(c=>c.outerHTML).join('');
    const attrs=Object.entries(this.attributes).map(([k,v])=>` ${{k}}=\"${{esc(v)}}\"`).join('');
    return `<${{this.tagName}}${{attrs}}>${{this.children.map(c=>c.outerHTML).join('')}}</${{this.tagName}}>`;
  }}
}}
globalThis.document={{
  createTextNode:text=>new FakeNode(3,'#text',String(text)),
  createDocumentFragment:()=>new FakeNode(11,'#fragment'),
}};
globalThis.DOMParser=class{{parseFromString(html){{
  const host=new FakeNode(1,'div');
  const node=new FakeNode(1,'span');
  node.setAttribute('class','media-node');
  const ref=(html.match(/data-ref=\"([^\"]*)\"/)||[])[1]||'';
  node.setAttribute('data-ref',ref);
  host.appendChild(node);
  return {{body:{{firstChild:host}}}};
}}}};
function requestAnimationFrame(cb){{cb();}}
{helpers}
const root=new FakeNode(1,'div');
const parser={{}};
const data={{}};
const writeText=(parent,_data,text)=>parent.appendChild(document.createTextNode(text));
for(const chunk of {json.dumps(chunks)}){{
  _smdMediaAwareAddText(null,root,data,chunk,_SMD_MEDIA_TAIL,parser,writeText);
}}
_smdMediaTailFlush(parser);
console.log(JSON.stringify({{html:root.outerHTML,text:root.textContent}}));
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "--eval", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


class TestSmdMediaInStream(unittest.TestCase):
    def test_media_aware_wrapper_exists(self):
        self.assertIn("function _smdMediaAwareAddText", MESSAGES_JS)
        self.assertIn("_inlineMediaHtmlForRef", MESSAGES_JS)

    def test_prefix_tail_logic_is_present(self):
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 7000]
        self.assertIn("const _SMD_MEDIA_PREFIX = 'MEDIA:'", MESSAGES_JS)
        self.assertIn("function _smdMediaPrefixTail", MESSAGES_JS)
        self.assertIn("_smdMediaPrefixTail(combined)", block)
        self.assertIn("_smdMediaPrefixTail(rest)", block)
        self.assertIn("_SMD_MEDIA_PREFIX.startsWith(suffix)", MESSAGES_JS)

    def test_partial_media_ref_at_chunk_end_is_buffered_until_boundary(self):
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("function _smdMediaRefHasReliableBoundary", MESSAGES_JS)
        self.assertIn("matchEnd===combined.length", block)
        self.assertIn("!_smdMediaRefHasReliableBoundary(parts?parts[0]:m[1])", block)
        self.assertLess(block.index("const parts="), block.index("if(matchEnd===combined.length"))
        self.assertIn("unmatchedTail = candidate", block)

    def test_media_ref_boundary_extension_list_matches_renderer_formats(self):
        idx = MESSAGES_JS.index("function _smdMediaRefHasReliableBoundary")
        block = MESSAGES_JS[idx:idx + 1200]
        # Match the implementation tokens, not one expanded spelling. `jpe?g`
        # intentionally covers both jpg and jpeg.
        for ext in ["png", "jpe?g", "svg", "mp4", "mp3", "pdf", "html?", "csv", "diff", "patch", "excalidraw"]:
            self.assertIn(ext, block)


@unittest.skipIf(NODE is None, "node is required for streaming MEDIA behavior tests")
class TestSmdMediaAwareAddTextBehaviour(unittest.TestCase):
    def test_partial_punctuation_split_reconstructs_reference(self):
        result = _run_media_aware_chunks(["MEDIA:/tmp/a.", "png "])
        self.assertIn('data-ref="/tmp/a.png"', result["html"])
        self.assertNotIn('data-ref="/tmp/a"', result["html"])
        self.assertNotIn("MEDIA:", result["text"])

    def test_remote_query_fragment_trailing_punctuation_is_preserved(self):
        for suffix in ["?signature=value!", "#section."]:
            with self.subTest(suffix=suffix):
                ref = f"https://example.com/a.png{suffix}"
                result = _run_media_aware_chunks([f"MEDIA:{ref} "])
                self.assertIn(f'data-ref="{ref}"', result["html"])


if __name__ == "__main__":
    unittest.main()
