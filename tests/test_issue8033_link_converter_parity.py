"""#8033 maintainer fold: the three file:// converters share one policy.

The PR applied the local-authority rule to the streaming link converter (messages.js ``_smdLinkHref``) and to media refs
(ui.js ``_inlineMediaHtmlForRef``), but the settled markdown link converter (ui.js ``_markdownHref``, the final DOM for
``[label](file://...)``) still rewired a UNC ``file://server/share/x`` link to ``api/media?path=server/share/x``. The
streaming converter also switched to the parsed pathname, dropping a ``#...``/``?...`` suffix that master and the settled
converter keep, so ``file:///C:/a#b.png`` streamed as ``/C:/a`` and settled as ``/C:/a#b.png``.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

_DRIVER = r"""
const fs = require('fs');
function extractFrom(path, name) {
  const src = fs.readFileSync(path, 'utf8');
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) { if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
eval(extractFrom(process.argv[1], '_markdownHref'));
eval(extractFrom(process.argv[2], '_smdLinkHref'));
const cases = JSON.parse(process.argv[3]);
const out = {};
for (const c of cases) out[c] = { settled: _markdownHref(c), streaming: _smdLinkHref(c) };
console.log(JSON.stringify(out));
"""

CASES = [
    "file:///C:/Users/me/report.png",
    "file:///C:/a#b.png",
    "file:///tmp/a#",
    "file:///tmp/a?",
    "file:///tmp/a?#b.png",
    "file:///tmp/a?x#",
    "file:///tmp/a%23b.png",
    "file:///tmp/x.png?raw=1",
    "file:///home/me/my%20report.png",
    "file://localhost/C:/x.png",
    "file://server/share/x.png",
    "file://server:abc/share/x.png",
]


@pytest.fixture(scope="module")
def results():
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(REPO / "static" / "ui.js"), str(REPO / "static" / "messages.js"), json.dumps(CASES)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _path(href):
    from urllib.parse import parse_qs, urlsplit
    assert href.startswith("api/media?"), href
    return parse_qs(urlsplit(href).query)["path"][0]


def test_settled_link_converter_rejects_unc_authority(results):
    assert results["file://server/share/x.png"]["settled"] == "file://server/share/x.png"
    assert results["file://server:abc/share/x.png"]["settled"] == "file://server:abc/share/x.png"


def test_streaming_link_converter_keeps_the_suffix(results):
    assert _path(results["file:///C:/a#b.png"]["streaming"]) == "/C:/a#b.png"
    assert _path(results["file:///tmp/x.png?raw=1"]["streaming"]) == "/tmp/x.png?raw=1"


@pytest.mark.parametrize("case", CASES)
def test_streaming_and_settled_links_agree(results, case):
    assert results[case]["settled"] == results[case]["streaming"], results[case]


def test_local_paths_unchanged(results):
    assert _path(results["file:///C:/Users/me/report.png"]["settled"]) == "/C:/Users/me/report.png"
    assert _path(results["file:///tmp/a%23b.png"]["settled"]) == "/tmp/a#b.png"
    assert _path(results["file:///home/me/my%20report.png"]["settled"]) == "/home/me/my report.png"
    assert _path(results["file://localhost/C:/x.png"]["settled"]) == "/C:/x.png"


def test_empty_delimiters_are_kept(results):
    """URL.search/hash drop an empty '?' or '#', which would select a different file ('a#' -> 'a')."""
    for case, expected in [
        ("file:///tmp/a#", "/tmp/a#"),
        ("file:///tmp/a?", "/tmp/a?"),
        ("file:///tmp/a?#b.png", "/tmp/a?#b.png"),
        ("file:///tmp/a?x#", "/tmp/a?x#"),
    ]:
        assert _path(results[case]["streaming"]) == expected, case
        assert _path(results[case]["settled"]) == expected, case
