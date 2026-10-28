"""Regression: two photos with the same generic filename must both attach.

Mobile camera/photo pickers (iOS Safari) name every photo "image.jpg".
addFiles() used to drop any file whose name matched one already pending, so
the second photo vanished silently. A genuine duplicate (same name, size and
mtime) is still skipped.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _run(files_js: str) -> list:
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    block = _extract(ui, "function _pendingFileUniqueName(", "const _uploadPendingFilesProgressBySession")
    source = f"""
class File extends Blob {{
  constructor(parts, name, options={{}}) {{
    super(parts, options);
    this.name = name;
    this.lastModified = options.lastModified === undefined ? Date.now() : options.lastModified;
  }}
}}
const MAX_UPLOAD_BYTES = 1e9;
const S = {{pendingFiles: []}};
function renderTray() {{}}
function _showUploadTooLarge() {{}}
{block}
const batches = {files_js};
for (const batch of batches) addFiles(batch.map(([n, body, mtime]) => new File([body], n, {{lastModified: mtime, type: 'image/jpeg'}})));
console.log(JSON.stringify(S.pendingFiles.map(f => [f.name, f.size, f.type, f.lastModified])));
"""
    result = subprocess.run([NODE], input=source, text=True, capture_output=True, cwd=REPO_ROOT, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def test_two_photos_same_name_both_attach_in_one_batch():
    out = _run('[[["image.jpg","aaa",1],["image.jpg","bbbb",2]]]')
    assert [f[0] for f in out] == ["image.jpg", "image-2.jpg"]
    assert [f[1] for f in out] == [3, 4]


def test_two_photos_same_name_added_in_separate_batches():
    out = _run('[[["image.jpg","aaa",1]],[["image.jpg","bbbb",2]]]')
    assert [f[0] for f in out] == ["image.jpg", "image-2.jpg"]


def test_three_photos_get_increasing_suffixes_and_keep_type():
    out = _run('[[["image.jpg","a",1],["image.jpg","bb",2],["image.jpg","ccc",3]]]')
    assert [f[0] for f in out] == ["image.jpg", "image-2.jpg", "image-3.jpg"]
    assert all(f[2] == "image/jpeg" for f in out)
    assert [f[3] for f in out] == [1, 2, 3]


def test_true_duplicate_is_still_skipped():
    out = _run('[[["image.jpg","aaa",5]],[["image.jpg","aaa",5]]]')
    assert [f[0] for f in out] == ["image.jpg"]


def test_renamed_alias_does_not_swallow_a_genuinely_different_file():
    # Maintainer review, row 1: A image.jpg (3B,1) -> B image.jpg (4B,2) -> C image-2.jpg (4B,2).
    # B is kept as the alias image-2.jpg; C is a different file that happens to
    # carry that name, so identity must use B's ORIGINAL name (image.jpg), not C's.
    out = _run('[[["image.jpg","aaa",1]],[["image.jpg","bbbb",2]],[["image-2.jpg","bbbb",2]]]')
    assert [f[1] for f in out] == [3, 4, 4]
    assert len(out) == 3
    assert len({f[0] for f in out}) == 3  # all staged names are distinct


def test_re_adding_a_renamed_photo_is_a_true_duplicate():
    # Maintainer review, row 2: A -> B -> re-add B. B must not be added a second time.
    out = _run('[[["image.jpg","aaa",1]],[["image.jpg","bbbb",2]],[["image.jpg","bbbb",2]]]')
    assert [f[0] for f in out] == ["image.jpg", "image-2.jpg"]


def test_compound_archive_extension_is_kept_whole():
    # Greptile P1: archive.tar-2.gz would stop matching _ARCHIVE_EXTS and skip extraction.
    out = _run('[[["archive.tar.gz","aaa",1],["archive.tar.gz","bbbb",2],["a.tar.bz2","x",1],["a.tar.bz2","yy",2],["a.tar.xz","x",1],["a.tar.xz","yy",2]]]')
    assert [f[0] for f in out] == [
        "archive.tar.gz", "archive-2.tar.gz", "a.tar.bz2", "a-2.tar.bz2", "a.tar.xz", "a-2.tar.xz",
    ]


def test_name_without_extension_and_dotfile():
    out = _run('[[["photo","a",1],["photo","bb",2],[".hidden","a",1],[".hidden","bb",2]]]')
    assert [f[0] for f in out] == ["photo", "photo-2", ".hidden", ".hidden-2"]
