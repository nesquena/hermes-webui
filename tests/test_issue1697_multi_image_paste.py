"""Regression coverage for #1697: multi-image clipboard paste attachments."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _read_js(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_msg_paste_registration() -> str:
    boot = _read_js(BOOT_JS_PATH)
    marker = "$('msg').addEventListener('paste',e=>{"
    start = boot.find(marker)
    assert start >= 0, "boot.js must register the composer paste handler"
    end_marker = "\n});"
    end = boot.find(end_marker, start)
    assert end >= 0, "composer paste handler should end with a listener close"
    return boot[start : end + len(end_marker)]


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout.strip()


def _paste_harness(items_js: str) -> dict:
    paste_registration = json.dumps(_extract_msg_paste_registration())
    source = f"""
const vm = require('vm');
const pasteRegistration = {paste_registration};
const listeners = {{}};
const S = {{pendingFiles: []}};
let renderCount = 0;
let lastStatus = '';
let preventDefaultCount = 0;
class File extends Blob {{
  constructor(parts, name, options={{}}) {{
    super(parts, options);
    this.name = name;
    this.lastModified = options.lastModified || 0;
  }}
}}
const context = {{
  S,
  File,
  Blob,
  Date: {{now: () => 1700000000000}},
  Array,
  console,
  $: (id) => {{
    if (id !== 'msg') throw new Error('unexpected element id '+id);
    return {{addEventListener: (type, cb) => {{listeners[type] = cb;}}}};
  }},
  addFiles: (files) => {{
    for (const f of files) {{
      if (!S.pendingFiles.find(p => p.name === f.name)) S.pendingFiles.push(f);
    }}
    renderCount += 1;
  }},
  setStatus: (text) => {{ lastStatus = text; }},
  t: (key) => key === 'image_pasted' ? 'Image pasted: ' : key,
}};
vm.createContext(context);
vm.runInContext(pasteRegistration, context);
listeners.paste({{
  clipboardData: {{items: {items_js}}},
  preventDefault: () => {{ preventDefaultCount += 1; }},
}});
console.log(JSON.stringify({{
  pendingNames: S.pendingFiles.map(f => f.name),
  pendingCount: S.pendingFiles.length,
  renderCount,
  lastStatus,
  preventDefaultCount,
}}));
"""
    return json.loads(_run_node(source))


def test_one_clipboard_paste_with_two_image_items_adds_two_attachment_chips():
    """Two image clipboard items from one paste must survive addFiles() filename de-dupe."""
    result = _paste_harness(
        "["
        "{kind:'file', type:'image/png', getAsFile:()=>new Blob(['one'], {type:'image/png'})},"
        "{kind:'file', type:'image/png', getAsFile:()=>new Blob(['two'], {type:'image/png'})}"
        "]"
    )

    assert result["preventDefaultCount"] == 1
    assert result["renderCount"] == 1
    assert result["pendingCount"] == 2
    assert result["pendingNames"] == [
        "screenshot-1700000000000-1.png",
        "screenshot-1700000000000-2.png",
    ]
    assert result["lastStatus"] == (
        "Image pasted: screenshot-1700000000000-1.png, "
        "screenshot-1700000000000-2.png"
    )


def test_single_image_paste_keeps_existing_screenshot_filename_shape():
    """The one-image path should keep screenshot-<timestamp>.<ext> for compatibility."""
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>new Blob(['one'], {type:'image/png'})}]"
    )

    assert result["pendingNames"] == ["screenshot-1700000000000.png"]


def test_file_picker_and_drop_paths_still_pass_real_file_names_to_addfiles():
    """Non-clipboard multi-file paths should preserve browser-provided filenames."""
    boot = _read_js(BOOT_JS_PATH)
    panels = _read_js(PANELS_JS_PATH)

    assert "$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};" in boot
    assert "const files=Array.from(e.dataTransfer.files);" in panels
    assert "if(files.length){addFiles(files);$('msg').focus();}" in panels
    assert "screenshot-" not in panels[panels.find("document.addEventListener('drop'") : panels.find("document.addEventListener('drop'") + 900]
