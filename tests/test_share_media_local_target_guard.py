"""Share-page MEDIA guard: an http(s) ref must not smuggle a local target.

Two halves of one privacy decision, pinned in both languages:

* ``api/helpers.py::external_media_url_hides_local_target`` — server side.
  Runs before a public snapshot is written. Strict: also rejects
  loopback/RFC 1918 hosts, because an anonymous viewer can never legitimately
  fetch one.
* ``static/ui.js::_externalMediaUrlHidesLocalTarget`` — client side, driven
  here through node against the REAL ``_inlineMediaHtmlForRef``. Marker-only:
  a private HOST alone stays renderable so the normal app keeps following a
  dev-server asset URL, which is why the two are deliberately asymmetric.

The client half matters because ``static/share.html`` loads ``ui.js`` and
``share.js`` calls ``renderMd()``, and the https:// branch of
``_inlineMediaHtmlForRef`` rewrites a loopback host to ``document.baseURI``.
On a share origin that turns ``MEDIA:http://127.0.0.1:8080/api/media?path=...``
into a same-origin AUTHENTICATED request from the viewer's browser. The server
guard stops new snapshots; this one stops snapshots written by older builds.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
DRIVER = Path(__file__).parent / "js" / "share_media_guard_driver.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_js_share_media_guard_matrix():
    """Drive the real ui.js renderer: attack shapes inert, legit refs intact."""
    result = subprocess.run(
        [NODE, str(DRIVER), str(UI_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "JS share-media guard matrix failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ALL OK" in result.stdout, result.stdout


# ── Python/JS parity on the marker half ──────────────────────────────────────
# Both languages must agree about which refs hide a local target. The private
# host rows are excluded here: that is the documented asymmetry (server-only),
# and it is asserted separately below.
_MARKER_PARITY_CASES = [
    ("https://cdn.test/a.png", False),
    ("https://cdn.test/a.png?w=800&fmt=webp", False),
    ("https://cdn.test/img/photo.png", False),
    ("https://cdn.test/a.png?next=/media/other.png", False),
    ("https://cdn.test/img/MEDIA:/etc/passwd.png", True),
    ("https://cdn.test/a.png?src=MEDIA:/etc/shadow.png", True),
    ("https://cdn.test/a.png#MEDIA:/etc/shadow.png", True),
    ("https://cdn.test/a.png?src=file:///etc/shadow.png", True),
    ("https://cdn.test/a.png?src=%4dEDIA:/etc/shadow.png", True),
    ("https://cdn.test/a.png?src=%254dEDIA:/etc/shadow.png", True),
    ("https://cdn.test/api/media?path=/etc/shadow", True),
    ("/tmp/local.png", False),
    ("file:///etc/shadow.png", False),
]


@pytest.mark.parametrize("ref,hides", _MARKER_PARITY_CASES)
def test_python_marker_verdicts(ref, hides):
    from api.helpers import external_media_url_hides_local_target

    assert external_media_url_hides_local_target(ref) is hides, ref


def test_js_marker_verdicts_match_python():
    """The JS predicate must return the same verdict as Python on every row."""
    from api.helpers import external_media_url_hides_local_target

    cases_js = ",".join(
        "[" + repr(ref).replace("'", '"') + "]" for ref, _ in _MARKER_PARITY_CASES
    )
    driver = f"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function extractFunc(name){{
  const re=new RegExp('function\\\\s+'+name+'\\\\s*\\\\(');
  const start=src.search(re);
  if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{{',start); let depth=1; i++;
  while(depth>0&&i<src.length){{
    if(src[i]==='{{')depth++; else if(src[i]==='}}')depth--; i++;
  }}
  return src.slice(start,i);
}}
eval(extractFunc('_unquoteMediaRef'));
eval(extractFunc('_localTargetMarkers'));
eval(extractFunc('_decodeUrlComponentBounded'));
eval(extractFunc('_externalMediaUrlHidesLocalTarget'));
const cases=[{cases_js}];
console.log(JSON.stringify(cases.map(c=>_externalMediaUrlHidesLocalTarget(c[0]))));
"""
    import tempfile, json, os

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(driver)
        path = fh.name
    try:
        result = subprocess.run(
            [NODE, path, str(UI_JS_PATH)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        js_verdicts = json.loads(result.stdout.strip())
    finally:
        os.unlink(path)

    for (ref, _expected), js in zip(_MARKER_PARITY_CASES, js_verdicts):
        py = external_media_url_hides_local_target(ref)
        # Python is stricter ONLY on private hosts, none of which appear here,
        # so on this set the two must agree exactly.
        assert py is js, f"parity break on {ref!r}: python={py} js={js}"


@pytest.mark.parametrize(
    "ref",
    [
        "http://127.0.0.1:8787/img/shot.png",
        "http://localhost:8787/img/shot.png",
        "http://192.168.1.5/x.png",
        "http://10.0.0.7/x.png",
        "http://172.16.0.9/x.png",
        "http://169.254.1.1/x.png",
        "http://[::1]/x.png",
    ],
)
def test_private_hosts_are_server_side_only_rejections(ref):
    """Documented asymmetry: server rejects a private host, client renders it.

    A published snapshot must never carry a loopback URL (an anonymous viewer
    would resolve it in their own network position), but the live app
    legitimately serves assets from a dev server, so the client must not break
    that. If this ever flips, the two halves have drifted and the scope note in
    both files is wrong.
    """
    from api.helpers import external_media_url_hides_local_target

    assert external_media_url_hides_local_target(ref) is True, (
        f"{ref}: server side must reject a private host for public shares"
    )


def test_malformed_url_fails_closed():
    from api.helpers import external_media_url_hides_local_target

    # No host at all — unparseable as a public asset, so it must not be
    # preserved into a snapshot.
    assert external_media_url_hides_local_target("http:///nohost.png") is True
