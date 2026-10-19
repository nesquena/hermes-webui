"""Regression test for PR #8212 — the subpath ``next`` remap must not open a
protocol-relative redirect (open redirect) after login.

Background
----------
The subpath remap in ``_safeNextPath()`` prepends the app root (derived
from ``location.pathname``) onto server-generated ``?next=`` values,
because a prefix-stripping reverse proxy removes the mount prefix
server-side while the browser keeps it.

Review findings (both gate the merge):

1. At ``//victim//login`` the derived app root is ``//`` — the server
   collapses ``//`` to ``/`` (CPython gh-87389) so the page serves, but
   the browser keeps the double slash. ``next=/evil.com`` then remapped
   to ``//evil.com``: a protocol-relative URL pointing at a *different
   origin*, navigated to on successful password/passkey login.
2. The input-side protocol-relative guard (reject a leading ``//``) only
   ever sees the *input* — never the remap output.

The fix normalises the app root (collapse doubled leading slashes) and
re-runs the protocol-relative/backslash guard on the remapped value,
falling back to ``./`` if it fails.

This test executes the *real* ``_safeNextPath`` from
``static/login.js`` in a small node harness (no Python
re-implementation that could drift) and asserts the invariant: the
returned value must never be protocol-relative.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGIN_JS = REPO_ROOT / "static" / "login.js"

NODE_HARNESS = r"""
const fs = require('fs');
const [,, jsPath, ...hrefs] = process.argv;
const js = fs.readFileSync(jsPath, 'utf8');
const marker = 'function _safeNextPath(';
const start = js.indexOf(marker);
if (start < 0) { console.error('function _safeNextPath not found'); process.exit(2); }
const brace = js.indexOf('{', start);
let depth = 1, i = brace + 1;
while (i < js.length && depth > 0) {
  if (js[i] === '{') depth++;
  else if (js[i] === '}') depth--;
  i++;
}
if (depth !== 0) { console.error('unbalanced braces in _safeNextPath'); process.exit(2); }
global.window = { location: { href: '' } };
const fn = eval('(' + js.slice(start, i) + ')');
const results = hrefs.map(href => {
  global.window.location.href = href;
  const got = fn();
  // Resolve the returned value the way the browser will navigate to it.
  return [got, new URL(got, href).origin === new URL(href).origin];
});
console.log(JSON.stringify(results));
"""

# href -> expected return value of _safeNextPath()
CASES: dict[str, str] = {
    # --- the review's attack vectors (must NOT leave the origin) ---
    # Core attack: browser keeps // on the path, app root derives as //,
    # next=/evil.com must NOT remap to //evil.com (protocol-relative).
    "https://victim//login?next=/evil.com": "/evil.com",
    # Review vector 2: no next at all -> stay put.
    "https://victim/evil.com/": "./",
    # Review vector 3: doubled-slash path, no next -> stay put.
    "https://victim//evil.com": "./",
    # The real chain: the //evil.com bounce lands on //login with next set.
    "https://victim//login?next=/evil.com/": "/evil.com/",
    "https://victim///login?next=/evil.com": "/evil.com",
    "https://victim//login?next=//evil.com": "./",
    # Slash-backslash and encoded/whitespace-prefixed protocol-relative forms.
    "https://victim//login?next=/\\evil.com": "./",
    "https://victim/hermes/login?next=/\\evil.com": "./",
    "https://victim//login?next=%2F%2Fevil.com": "./",
    "https://victim/hermes/login?next=%2F%09%2Fevil.com": "./",
    "https://victim//login?next=%0A/evil.com": "./",
    "https://victim/hermes/login?next=https://evil.com": "./",
    "https://victim/hermes/login?next=javascript:alert(1)": "./",
    # Protocol-relative input was always rejected -> stays rejected.
    "https://victim/hermes/login?next=//evil.com": "./",
    # Backslash protocol-relative -> stays rejected.
    "https://victim/hermes/login?next=\\evil.com": "./",
    # --- normal subpath behaviour (the fix under test must keep working) ---
    "https://victim/hermes/login?next=/": "/hermes/",
    "https://victim/hermes/login?next=/settings": "/hermes/settings",
    # Frontend-generated next (already prefixed) passes through untouched.
    "https://victim/hermes/login?next=/hermes/chat/5": "/hermes/chat/5",
    # --- root deployment must be a no-op of the remap ---
    "https://x/login?next=/": "/",
    "https://x/login?next=/settings": "/settings",
}


def _run_node(harness: Path, hrefs: list[str]) -> list[str]:
    proc = subprocess.run(
        [str(shutil.which("node")), str(harness), str(LOGIN_JS), *hrefs],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.timeout(120)
def test_safenextpath_never_returns_protocol_relative(tmp_path: Path) -> None:
    """The review's invariant, over the full case table.

    For every href, the real ``_safeNextPath`` must return exactly the
    expected value, and — the security invariant — no result may start
    with ``//`` (protocol-relative, different origin) or ``/\\``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")

    hrefs = list(CASES)
    results = _run_node(harness, hrefs)
    assert len(results) == len(hrefs)

    failures = []
    for href, (got, same_origin), want in zip(hrefs, results, CASES.values(), strict=True):
        if got != want:
            failures.append(f"  {href}\n    got  {got!r}\n    want {want!r}")
        if got.startswith("//") or got.startswith("/\\"):
            failures.append(f"  {href}\n    PROTOCOL-RELATIVE (open redirect): {got!r}")
        if not same_origin:
            failures.append(f"  {href}\n    LEAVES THE ORIGIN when the browser navigates: {got!r}")
    assert not failures, "case-table failures:\n" + "\n".join(failures)
