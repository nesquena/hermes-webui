"""Regression coverage: clipboard image items whose getAsFile() returns null.

Some clipboard producers (delayed-render screenshot tools, cloud clipboards,
and a few browser clipboard implementations) advertise an ``image/*`` item but
return ``null`` from ``getAsFile()`` — the data never materialized.

The pre-fix code did ``new File([blob], ...)`` unconditionally. In JS,
``[null]`` stringifies to ``"null"``, so that path silently uploaded a 4-byte
file whose content is the literal text ``null`` and whose name still looked
like a real screenshot. The user saw a broken image attachment and no error.

The fix skips null blobs (``if(!blob)return null`` + ``.filter(Boolean)``) and
surfaces a visible failure status when every item was skipped, instead of
pretending the paste succeeded.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Content of `new File([null], ...)`: JS stringifies null to these four bytes.
FAKE_NULL_BYTES = 4


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
    """Drive the real composer paste handler with a synthetic clipboard."""
    paste_registration = json.dumps(_extract_msg_paste_registration())
    source = f"""
const vm = require('vm');
const pasteRegistration = {paste_registration};
const listeners = {{}};
const S = {{pendingFiles: []}};
let renderCount = 0;
let lastStatus = '';
let lastStatusType = '';
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
  setStatus: (text, type) => {{ lastStatus = text; lastStatusType = type || ''; }},
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
  pendingSizes: S.pendingFiles.map(f => f.size),
  pendingCount: S.pendingFiles.length,
  renderCount,
  lastStatus,
  lastStatusType,
  preventDefaultCount,
}}));
"""
    return json.loads(_run_node(source))


def test_null_getasfile_item_is_not_fabricated_into_a_file():
    """A null blob must be skipped, not turned into a 4-byte "null" file."""
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>null}]"
    )

    assert result["pendingCount"] == 0, result
    assert result["renderCount"] == 0, "addFiles must not be called with no real files"
    assert FAKE_NULL_BYTES not in result["pendingSizes"], result["pendingSizes"]
    assert result["lastStatusType"] == "error", result["lastStatusType"]


def test_all_null_items_surface_a_visible_failure_status():
    """When every item is null the user must see an error, not a silent success."""
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>null},"
        "{kind:'file', type:'image/jpeg', getAsFile:()=>null}]"
    )

    assert result["pendingCount"] == 0, result
    assert result["renderCount"] == 0, result
    # Harness `t()` returns the key verbatim for unknown keys, so the exact
    # expectation is the i18n key itself.
    assert result["lastStatus"] == "image_paste_failed", result["lastStatus"]
    # The failure must surface as an 'error' toast, not the inferred default.
    assert result["lastStatusType"] == "error", result["lastStatusType"]


def test_mixed_null_and_real_items_attach_only_the_real_one():
    """Null items are dropped individually; real siblings still attach."""
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>null},"
        "{kind:'file', type:'image/png', getAsFile:()=>new Blob(['real'], {type:'image/png'})}]"
    )

    assert result["pendingCount"] == 1, result
    assert result["pendingSizes"] == [4]  # len("real") — the only real payload
    assert result["pendingNames"] == ["screenshot-1700000000000-2.png"], result
    assert result["lastStatus"] == "Image pasted: screenshot-1700000000000-2.png"


def test_empty_blob_item_is_not_fabricated_into_a_file():
    """A declared image whose payload is zero bytes must be skipped.

    Real browsers do not always return ``null`` for a clipboard image item
    whose data never materialized — Safari and Chromium can hand back a
    ``Blob`` whose ``size`` is ``0``. That object is truthy, so a guard that
    only tests ``!blob`` lets it through and attaches a zero-byte file named
    like a screenshot, with a success status and no error.
    """
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>new Blob([], {type:'image/png'})}]"
    )

    assert result["pendingCount"] == 0, result
    assert result["renderCount"] == 0, "addFiles must not be called for an empty blob"
    assert 0 not in result["pendingSizes"], result["pendingSizes"]
    assert result["lastStatus"] == "image_paste_failed", result["lastStatus"]
    assert result["lastStatusType"] == "error", result["lastStatusType"]


def test_mixed_empty_and_real_items_attach_only_the_real_one():
    """An empty blob is dropped individually; a real sibling still attaches."""
    result = _paste_harness(
        "[{kind:'file', type:'image/png', getAsFile:()=>new Blob([], {type:'image/png'})},"
        "{kind:'file', type:'image/jpeg', getAsFile:()=>new Blob(['real'], {type:'image/jpeg'})}]"
    )

    assert result["pendingCount"] == 1, result
    assert result["pendingSizes"] == [4], result["pendingSizes"]
    assert result["lastStatus"].startswith("Image pasted:"), result["lastStatus"]


def test_source_guards_the_null_blob_path():
    """Pin the structural guards so a refactor cannot silently drop them."""
    boot = _read_js(BOOT_JS_PATH)

    assert "if(!blob||blob.size===0)return null;" in boot
    assert "}).filter(Boolean);" in boot
