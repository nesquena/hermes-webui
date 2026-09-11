"""#6224 frontend regression: the Markdown inline loader must keep the session grant.

`loadMarkdownInline()` fetches a rendered `.md` artifact through `/api/media`.
Out-of-root artifacts are only served when the request carries the `session_id`
grant that matches a `MEDIA:` token the session actually emitted, so *every* URL
the loader builds — the fetch URL and the download link on the success, oversize
and error paths — has to carry `session_id`. A download link that drops the
grant turns the preview into a 403.

These tests execute the real `loadMarkdownInline()` from `static/ui.js` under
node with `fetch`, `document` and the render helpers stubbed, then assert the
URLs that were actually requested / rendered. Same node-harness style as
tests/test_5306_subagent_sidebar_flicker.py.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.env.SCENARIO_MD);

let fetchedUrl = null;
let renderCalls = 0;
const element = {
  dataset: {path: scenario.path},
  attrs: {},
  outerHTML: '',
  setAttribute(k, v){ this.attrs[k] = v; },
};

const container = {querySelectorAll: () => [element]};
const document = {querySelectorAll: () => [element]};

const S = scenario.sessionId ? {session: {session_id: scenario.sessionId}} : {session: null};
const MD_MAX_SIZE = scenario.maxSize;
function esc(s){ return String(s); }
function renderMd(text){ renderCalls += 1; return '<p class="rendered">' + text + '</p>'; }
function t(key){ return key; }
let fetchError = null;
function fetch(url){
  fetchedUrl = url;
  if (scenario.network === 'reject') return Promise.reject(new Error('network down'));
  return Promise.resolve({
    ok: scenario.ok !== false,
    status: scenario.status || 200,
    text: async () => scenario.text,
  });
}

loadMarkdownInline(container);

new Promise(resolve => setTimeout(resolve, 0))
  .then(() => new Promise(resolve => setTimeout(resolve, 0)))
  .then(() => {
    console.log(JSON.stringify({
      url: fetchedUrl,
      outerHTML: element.outerHTML,
      renderCalls: renderCalls,
      loaded: element.attrs['data-loaded'] || null,
    }));
  })
  .catch(e => { console.log(JSON.stringify({error: String(e && e.message || e)})); });
"""

_FUNCTION_NAME = "loadMarkdownInline"


def _extract_function(source: str) -> str:
    from tests.js_source_extract import extract_function

    return extract_function(source, _FUNCTION_NAME)


def _run(
    text="short body",
    session_id="s-grant-123",
    ok=True,
    status=200,
    network="ok",
    max_size=4000,
) -> dict:
    fn = _extract_function(UI_JS.read_text(encoding="utf-8"))
    assert fn.startswith(f"function {_FUNCTION_NAME}("), "extracted the wrong function"
    source = _HARNESS.replace("/*__FUNCTION__*/", "") + fn
    env = dict(os.environ)
    env["SCENARIO_MD"] = json.dumps(
        {
            "path": "/opt/agent/work/notes.md",
            "text": text,
            "sessionId": session_id,
            "ok": ok,
            "status": status,
            "network": network,
            "maxSize": max_size,
        }
    )
    proc = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}\n{proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


def _download_href(outer_html: str) -> str:
    match = re.search(r'class="msg-media-link" href="([^"]+)"', outer_html)
    assert match, f"no media download link in rendered HTML: {outer_html!r}"
    return match.group(1)


def test_fetch_url_and_download_link_carry_the_session_grant():
    out = _run(session_id="s-grant-123")

    assert "path=%2Fopt%2Fagent%2Fwork%2Fnotes.md" in out["url"], out["url"]
    assert "session_id=s-grant-123" in out["url"], out["url"]

    href = _download_href(out["outerHTML"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert out["renderCalls"] == 1, "a previewable file must be rendered, not fall back"
    assert "md_download" in out["outerHTML"], "the download label must come from i18n"


def test_oversize_download_link_retains_the_session_grant():
    out = _run(text="x" * 64, session_id="s-grant-123", max_size=16)

    assert "session_id=s-grant-123" in out["url"], out["url"]
    assert out["renderCalls"] == 0, "an oversize file must not be rendered"
    href = _download_href(out["outerHTML"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert "md_too_large" in out["outerHTML"]


def test_error_download_link_retains_the_session_grant():
    out = _run(session_id="s-grant-123", ok=False, status=403)

    assert "session_id=s-grant-123" in out["url"], out["url"]
    href = _download_href(out["outerHTML"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert "md_error" in out["outerHTML"]


def test_network_failure_download_link_retains_the_session_grant():
    out = _run(session_id="s-grant-123", network="reject")

    assert "session_id=s-grant-123" in out["url"], out["url"]
    href = _download_href(out["outerHTML"])
    assert "session_id=s-grant-123" in href, href
    assert "md_error" in out["outerHTML"]


def test_public_urls_omit_the_grant_without_an_active_session():
    out = _run(session_id=None)

    assert "session_id=" not in out["url"], out["url"]
    assert "session_id=" not in _download_href(out["outerHTML"])
