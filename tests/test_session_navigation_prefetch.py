"""Behavioral coverage for speculative session-navigation prefetch failures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _function_block(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rejected_prefetch_is_evicted_and_click_retries_fresh_api():
    """A rejected speculative promise must not reject the real navigation."""
    source = SESSIONS_JS.read_text(encoding="utf-8")
    api_session_nav = _function_block(source, "_apiSessionNav")
    driver = f"""
const _SESSION_NAV_CACHE_TTL_MS = 20000;
const _sessionNavCache = new Map();
function _sessionNavRowIsStreaming() {{ return false; }}
let calls = 0;
function api(url, opts) {{
  calls += 1;
  return Promise.resolve({{fresh: true, url, opts}});
}}
{api_session_nav}
(async () => {{
  const sid = 'rejected-prefetch';
  const url = '/api/session?session_id=rejected-prefetch&messages=0&resolve_model=0';
  const rejected = Promise.reject(new Error('prefetch failed'));
  rejected.catch(() => {{}});
  const sibling = Promise.resolve({{stale: true}});
  const entry = {{at: Date.now(), urls: new Map([[url, rejected], ['tail', sibling]])}};
  _sessionNavCache.set(sid, entry);

  const result = await _apiSessionNav(sid, url, {{timeoutMs: 120000}});
  console.log(JSON.stringify({{
    result,
    calls,
    cachePresent: _sessionNavCache.has(sid),
    consumedUrlPresent: entry.urls.has(url),
    siblingStillPresent: entry.urls.has('tail'),
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    proc = subprocess.run(
        [NODE, "-e", driver],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    assert observed == {
        "result": {
            "fresh": True,
            "url": "/api/session?session_id=rejected-prefetch&messages=0&resolve_model=0",
            "opts": {"timeoutMs": 120000},
        },
        "calls": 1,
        "cachePresent": False,
        "consumedUrlPresent": False,
        "siblingStillPresent": True,
    }
