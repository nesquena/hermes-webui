"""#6559 — per-tab profile context: fail-closed resolution and tab isolation.

Round 2 of the maintainer review found three escape paths out of the per-tab
profile isolation this feature promises. Each has coverage here:

1. Fail-open on an expired context. The resolver used to treat an unresolvable
   ``tab_context`` as if none had been supplied and fell back to the shared
   ``hermes_profile`` cookie, so a dormant tab silently resumed under whichever
   profile the cookie currently named.
2. New-tab identity inversion. Profile links embedded the CURRENT tab's token
   in an href naming a DIFFERENT profile, and boot kept an inherited
   sessionStorage token instead of binding a fresh one to the target profile.
3. A bypassed ``EventSource`` — an SSE stream constructed without the tab
   context, which therefore resolved through the cookie.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
STATIC_DIR = REPO_ROOT / "static"
WORKSPACE_JS = (STATIC_DIR / "workspace.js").read_text(encoding="utf-8")
MESSAGES_JS = (STATIC_DIR / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (STATIC_DIR / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (STATIC_DIR / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (STATIC_DIR / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


# ── Server-side helpers ──────────────────────────────────────────────────────

class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for the profile resolver."""

    def __init__(self, path: str = "/api/sessions", cookie_profile: str | None = None):
        self.path = path
        self.headers = {}
        if cookie_profile is not None:
            self.headers["Cookie"] = f"hermes_profile={cookie_profile}"
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self

    # -- response capture --
    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


@pytest.fixture
def profiles_api(monkeypatch):
    """api.profiles with an empty tab-context map and auth disabled."""
    from api import auth as auth_api
    from api import profiles as profiles_module

    monkeypatch.setattr(auth_api, "is_auth_enabled", lambda: False)
    with profiles_module._TAB_CONTEXT_LOCK:
        profiles_module._TAB_CONTEXT_MAP.clear()
    try:
        yield profiles_module
    finally:
        with profiles_module._TAB_CONTEXT_LOCK:
            profiles_module._TAB_CONTEXT_MAP.clear()


def _expire(profiles_module, token: str) -> None:
    """Drive the token past its TTL by rewriting the map under the lock."""
    with profiles_module._TAB_CONTEXT_LOCK:
        profile_name, _expiry = profiles_module._TAB_CONTEXT_MAP[token]
        profiles_module._TAB_CONTEXT_MAP[token] = (profile_name, time.time() - 1)


# ── Defect 1: fail-closed resolution ─────────────────────────────────────────

def test_two_tabs_resolve_independently_and_an_expired_one_never_uses_the_cookie(profiles_api):
    """Two tabs, two profiles, one dead token — and no silent cookie fallback.

    The shared cookie names 'alpha' throughout, which is exactly the value the
    old resolver leaked to tab B once its token aged out.
    """
    token_a = profiles_api.issue_tab_context("alpha")
    token_b = profiles_api.issue_tab_context("beta")
    assert token_a != token_b

    # Both tabs resolve to their OWN profile while their tokens are alive, even
    # though the browser-wide cookie says 'alpha' for both of them.
    for token, expected in ((token_a, "alpha"), (token_b, "beta")):
        handler = _FakeHandler(f"/api/sessions?tab_context={token}", cookie_profile="alpha")
        resolution = profiles_api.resolve_profile_with_tab_context(handler)
        assert resolution.profile == expected
        assert resolution.invalid_tab_context is False

    # Tab B goes idle past the TTL.
    _expire(profiles_api, token_b)

    expired = _FakeHandler(f"/api/sessions?tab_context={token_b}", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(expired)
    assert resolution.invalid_tab_context is True, "expired tab context must be reported, not swallowed"
    assert resolution.profile is None, "expired tab context must NOT resolve through the cookie"

    # Tab A is unaffected by its neighbour dying.
    still_alive = _FakeHandler(f"/api/sessions?tab_context={token_a}", cookie_profile="alpha")
    assert profiles_api.resolve_profile_with_tab_context(still_alive).profile == "alpha"

    # A request that supplies NO tab context keeps the historical cookie path.
    cookie_only = _FakeHandler("/api/sessions", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(cookie_only)
    assert resolution.profile == "alpha"
    assert resolution.invalid_tab_context is False


def test_unknown_and_blank_tab_contexts_are_invalid_not_absent(profiles_api):
    unknown = _FakeHandler("/api/sessions?tab_context=never-issued", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(unknown)
    assert (resolution.profile, resolution.invalid_tab_context) == (None, True)

    # A present-but-empty parameter is a supplied context the server cannot
    # resolve; it must not degrade to "no context supplied".
    blank = _FakeHandler("/api/sessions?tab_context=", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(blank)
    assert (resolution.profile, resolution.invalid_tab_context) == (None, True)


def test_url_profile_still_outranks_tab_context(profiles_api):
    token = profiles_api.issue_tab_context("alpha")
    handler = _FakeHandler(f"/api/sessions?tab_context={token}", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(handler, url_profile="beta")
    assert resolution.profile == "beta"
    assert resolution.invalid_tab_context is False


def test_resolving_a_live_token_refreshes_its_ttl(profiles_api):
    token = profiles_api.issue_tab_context("alpha")
    with profiles_api._TAB_CONTEXT_LOCK:
        _name, first_expiry = profiles_api._TAB_CONTEXT_MAP[token]
        profiles_api._TAB_CONTEXT_MAP[token] = (_name, time.time() + 1)
    assert profiles_api.resolve_tab_context(token) == "alpha"
    with profiles_api._TAB_CONTEXT_LOCK:
        _name, refreshed = profiles_api._TAB_CONTEXT_MAP[token]
    assert refreshed > first_expiry - profiles_api._TAB_CONTEXT_TTL


def test_invalid_tab_context_is_answered_with_an_explicit_error(profiles_api):
    """The transport turns an unresolvable context into 409, not into a
    request served under someone else's profile."""
    token = profiles_api.issue_tab_context("beta")
    _expire(profiles_api, token)

    handler = _FakeHandler(f"/api/sessions?tab_context={token}", cookie_profile="alpha")
    resolution = profiles_api.resolve_profile_with_tab_context(handler)
    rejected = profiles_api.reject_invalid_tab_context(
        handler, resolution, urlparse(handler.path)
    )
    assert rejected is True
    assert handler.status == 409
    assert handler.get_json() == {"error": profiles_api.TAB_CONTEXT_INVALID_ERROR}


def test_valid_and_absent_tab_contexts_are_not_rejected(profiles_api):
    token = profiles_api.issue_tab_context("beta")
    for path in (f"/api/sessions?tab_context={token}", "/api/sessions"):
        handler = _FakeHandler(path, cookie_profile="alpha")
        resolution = profiles_api.resolve_profile_with_tab_context(handler)
        assert profiles_api.reject_invalid_tab_context(
            handler, resolution, urlparse(handler.path)
        ) is False
        assert handler.status is None


def test_reissue_endpoint_is_exempt_from_the_invalid_context_error(profiles_api):
    """The recovery path must stay reachable to a tab that still holds a dead
    token, or the tab can never get a live one."""
    handler = _FakeHandler(
        f"{profiles_api.TAB_CONTEXT_ENDPOINT_PATH}?tab_context=never-issued",
        cookie_profile="alpha",
    )
    resolution = profiles_api.resolve_profile_with_tab_context(handler)
    assert resolution.invalid_tab_context is True
    assert profiles_api.reject_invalid_tab_context(
        handler, resolution, urlparse(handler.path)
    ) is False
    assert handler.status is None


def _drive_handler(monkeypatch, profiles_api, path, *, method="GET"):
    """Run a request through the real server transport with auth stubbed out."""
    import server

    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.command = method
    handler._req_t0 = 0
    handler.headers = {"Cookie": "hermes_profile=alpha"}
    handler.status = None
    handler.body = bytearray()
    handler.wfile = handler
    handler.send_response = lambda code: setattr(handler, "status", code)
    handler.send_header = lambda *_a: None
    handler.end_headers = lambda: None
    handler.write = lambda data: handler.body.extend(
        data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
    )

    routed = {}

    def fake_route(_handler, parsed):
        routed["path"] = parsed.path
        # A route that ran under the WRONG profile is the bug; record what it saw.
        routed["profile"] = profiles_api.get_active_profile_name()
        return True

    monkeypatch.setattr("server.check_auth", lambda *_a: True)
    monkeypatch.setattr("server.reset_trusted_auth_request_state", lambda *_a: None)
    if method == "GET":
        monkeypatch.setattr("server.handle_get", fake_route)
        server.Handler.do_GET(handler)
    else:
        server.Handler._handle_write(handler, fake_route)
    return handler, routed


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_refuses_a_dead_tab_context_instead_of_routing_it(monkeypatch, profiles_api, method):
    """End-to-end: an expired context must never reach a route handler, on
    reads or writes. Routing it is what served a dormant tab under the cookie
    profile."""
    token = profiles_api.issue_tab_context("beta")
    _expire(profiles_api, token)

    handler, routed = _drive_handler(
        monkeypatch, profiles_api, f"/api/sessions?tab_context={token}", method=method
    )
    assert handler.status == 409
    assert json.loads(handler.body.decode("utf-8")) == {"error": "tab_context_invalid"}
    assert routed == {}, "the request must not reach the route under any profile"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_routes_a_live_tab_context_under_its_own_profile(monkeypatch, profiles_api, method):
    token = profiles_api.issue_tab_context("beta")
    handler, routed = _drive_handler(
        monkeypatch, profiles_api, f"/api/sessions?tab_context={token}", method=method
    )
    assert handler.status is None
    assert routed["path"] == "/api/sessions"
    assert routed["profile"] == "beta", "the route must run under the tab's profile, not the cookie's"


# ── Reissue binding ──────────────────────────────────────────────────────────

@pytest.fixture
def known_profiles(monkeypatch, profiles_api):
    monkeypatch.setattr(
        profiles_api,
        "list_profiles_api",
        lambda: [{"name": "default"}, {"name": "alpha"}, {"name": "beta"}],
    )
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "alpha")
    return profiles_api


def _call_tab_context_endpoint(query: str = ""):
    from api import routes

    path = "/api/profile/tab-context" + (f"?{query}" if query else "")
    handler = _FakeHandler(path)
    routes.handle_get(handler, urlparse(path))
    return handler


def test_reissue_binds_to_the_profile_the_client_declares(known_profiles):
    """The leak this closes: an expired tab running 'beta' must not come back
    as 'alpha' just because the shared cookie resolves to 'alpha'."""
    handler = _call_tab_context_endpoint("profile=beta")
    payload = handler.get_json()
    assert payload["profile"] == "beta"
    assert known_profiles.resolve_tab_context(payload["token"]) == "beta"


def test_reissue_rejects_a_profile_the_server_does_not_know(known_profiles):
    handler = _call_tab_context_endpoint("profile=ghost")
    assert handler.status == 400
    assert handler.get_json() == {"error": "unknown_profile"}
    with known_profiles._TAB_CONTEXT_LOCK:
        assert not known_profiles._TAB_CONTEXT_MAP, "no token may be issued for an unknown profile"


def test_reissue_without_a_declared_profile_uses_the_active_profile(known_profiles):
    handler = _call_tab_context_endpoint()
    payload = handler.get_json()
    assert payload["profile"] == "alpha"
    assert known_profiles.resolve_tab_context(payload["token"]) == "alpha"


def test_is_known_profile_name_rejects_junk_and_enumeration_failure(known_profiles, monkeypatch):
    assert known_profiles.is_known_profile_name("beta") is True
    assert known_profiles.is_known_profile_name("default") is True
    assert known_profiles.is_known_profile_name("ghost") is False
    assert known_profiles.is_known_profile_name("../../etc") is False
    assert known_profiles.is_known_profile_name("") is False
    assert known_profiles.is_known_profile_name(None) is False

    def _boom():
        raise RuntimeError("profiles unavailable")

    monkeypatch.setattr(known_profiles, "list_profiles_api", _boom)
    assert known_profiles.is_known_profile_name("beta") is False, "fail closed when profiles can't be listed"


# ── Client-side: node-executed behavior ──────────────────────────────────────

nodeonly = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _tab_context_module_source() -> str:
    """The self-contained tab-context block at the top of workspace.js."""
    start = WORKSPACE_JS.index("// ── Per-tab profile context (#6559)")
    end = WORKSPACE_JS.index("async function api(path,opts={})")
    return WORKSPACE_JS[start:end]


def _extract_func(src: str, name: str) -> str:
    """Slice one top-level function declaration out of a JS source file.

    Brace counting skips string/template literals and comments so a default
    parameter value (``opts={}``) or a brace inside a string cannot truncate
    the slice.
    """
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if match is None:
        raise AssertionError(f"{name} not found")
    start = match.start()

    # Walk past the parameter list to the body's opening brace.
    i = match.end() - 1
    paren = 0
    while i < len(src):
        if src[i] == "(":
            paren += 1
        elif src[i] == ")":
            paren -= 1
            if paren == 0:
                break
        i += 1
    i = src.index("{", i) + 1

    depth = 1
    while depth > 0 and i < len(src):
        ch = src[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                i += 2 if src[i] == "\\" else 1
        elif ch == "/" and i + 1 < len(src) and src[i + 1] == "/":
            i = src.find("\n", i)
            if i < 0:
                break
        elif ch == "/" and i + 1 < len(src) and src[i + 1] == "*":
            i = src.index("*/", i) + 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _node_env(*, search: str, stored: dict[str, str]) -> str:
    """Browser-ish globals: a sessionStorage, a location, and a fetch recorder."""
    return f"""
const STORE = {json.dumps(stored)};
globalThis.sessionStorage = {{
  getItem(k) {{ return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; }},
  setItem(k, v) {{ STORE[k] = String(v); }},
  removeItem(k) {{ delete STORE[k]; }},
}};
globalThis.location = {{
  search: {json.dumps(search)},
  pathname: '/app/',
  href: 'https://example.test/app/' + {json.dumps(search)},
}};
globalThis.document = {{ baseURI: 'https://example.test/app/' }};
globalThis.window = {{ location: globalThis.location }};
const ISSUE_CALLS = [];
globalThis.fetch = async (url) => {{
  ISSUE_CALLS.push(url);
  const parsed = new URL(url, 'https://example.test/app/');
  const declared = parsed.searchParams.get('profile');
  return {{
    ok: true,
    headers: {{ get: () => 'application/json' }},
    json: async () => ({{ token: 'SERVER-TOKEN-FOR-' + (declared || 'cookie'), profile: declared }}),
  }};
}};
"""


@nodeonly
def test_copied_session_storage_boot_binds_a_distinct_context_to_the_target_profile():
    """Defect 2: a tab opened as ?profile=beta must NOT keep the opener's token.

    A new browsing context can inherit a copy of the opener's sessionStorage.
    The inherited token is bound to profile 'alpha' and outranks the cookie, so
    without the drop-and-rebind the new tab would serve every request as alpha
    while claiming to be beta.
    """
    module = _tab_context_module_source()
    source = _node_env(
        search="?profile=beta",
        stored={
            "hermes-tab-profile-ctx": "INHERITED-ALPHA-TOKEN",
            "hermes-tab-profile-ctx-profile": "alpha",
        },
    ) + """
(0, eval)(""" + json.dumps(module) + """);
const afterInheritDrop = sessionStorage.getItem('hermes-tab-profile-ctx');
(async () => {
  // boot: switchToProfile('beta') rebinds this tab's context...
  await _rebindTabContext('beta');
  // ...and boot awaits a bound context before any profile-sensitive request.
  await _ensureTabContextForBoot('beta');
  const requestUrl = _tabContextUrl('https://example.test/app/api/sessions');
  console.log(JSON.stringify({
    afterInheritDrop,
    token: sessionStorage.getItem('hermes-tab-profile-ctx'),
    boundProfile: sessionStorage.getItem('hermes-tab-profile-ctx-profile'),
    issueCalls: ISSUE_CALLS,
    requestUrl,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    payload = json.loads(_run_node(source))
    assert payload["afterInheritDrop"] is None, "an inherited opener token must be dropped at parse time"
    assert payload["token"] == "SERVER-TOKEN-FOR-beta"
    assert payload["token"] != "INHERITED-ALPHA-TOKEN", "the new tab must hold a DISTINCT token"
    assert payload["boundProfile"] == "beta"
    assert payload["issueCalls"], "the tab must issue its own context"
    for call in payload["issueCalls"]:
        assert "profile=beta" in call, f"every reissue must declare beta, got {call}"
    assert "tab_context=SERVER-TOKEN-FOR-beta" in payload["requestUrl"]


@nodeonly
def test_boot_keeps_an_existing_context_already_bound_to_this_tabs_profile():
    """A plain reload must not churn a healthy, correctly-bound context."""
    module = _tab_context_module_source()
    source = _node_env(
        search="",
        stored={
            "hermes-tab-profile-ctx": "LIVE-BETA-TOKEN",
            "hermes-tab-profile-ctx-profile": "beta",
        },
    ) + """
(0, eval)(""" + json.dumps(module) + """);
(async () => {
  await _ensureTabContextForBoot('beta');
  console.log(JSON.stringify({
    token: sessionStorage.getItem('hermes-tab-profile-ctx'),
    issueCalls: ISSUE_CALLS,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    payload = json.loads(_run_node(source))
    assert payload["token"] == "LIVE-BETA-TOKEN"
    assert payload["issueCalls"] == []


@nodeonly
def test_boot_rebinds_a_context_bound_to_a_different_profile():
    module = _tab_context_module_source()
    source = _node_env(
        search="",
        stored={
            "hermes-tab-profile-ctx": "STALE-ALPHA-TOKEN",
            "hermes-tab-profile-ctx-profile": "alpha",
        },
    ) + """
(0, eval)(""" + json.dumps(module) + """);
(async () => {
  await _ensureTabContextForBoot('beta');
  console.log(JSON.stringify({
    token: sessionStorage.getItem('hermes-tab-profile-ctx'),
    boundProfile: sessionStorage.getItem('hermes-tab-profile-ctx-profile'),
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    payload = json.loads(_run_node(source))
    assert payload["token"] == "SERVER-TOKEN-FOR-beta"
    assert payload["boundProfile"] == "beta"


@nodeonly
def test_api_wrapper_clears_reissues_and_retries_once_on_tab_context_invalid():
    """Defect 1, client half: the 409 must be recovered, not surfaced.

    The stale token is refused; api() has to clear it, reissue against the
    profile THIS tab declares (beta — not the cookie's alpha), and replay the
    same request carrying the new token.
    """
    module = _tab_context_module_source()
    api_func = _extract_func(WORKSPACE_JS, "api")
    source = """
const STORE = {
  'hermes-tab-profile-ctx': 'STALE-BETA-TOKEN',
  'hermes-tab-profile-ctx-profile': 'beta',
};
globalThis.sessionStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem(k, v) { STORE[k] = String(v); },
  removeItem(k) { delete STORE[k]; },
};
globalThis.location = { search: '', pathname: '/app/', href: 'https://example.test/app/' };
globalThis.document = { baseURI: 'https://example.test/app/' };
globalThis.window = { location: globalThis.location };
globalThis.showToast = () => {};
globalThis.S = { activeProfile: 'beta' };
const REQUESTS = [];
globalThis.fetch = async (url) => {
  REQUESTS.push(url);
  const parsed = new URL(url, 'https://example.test/app/');
  if (parsed.pathname.endsWith('/api/profile/tab-context')) {
    const declared = parsed.searchParams.get('profile');
    return {
      ok: true,
      headers: { get: () => 'application/json' },
      json: async () => ({ token: 'FRESH-TOKEN-FOR-' + declared, profile: declared }),
    };
  }
  const ctx = parsed.searchParams.get('tab_context');
  if (ctx === 'STALE-BETA-TOKEN') {
    return {
      ok: false,
      status: 409,
      statusText: 'Conflict',
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify({ error: 'tab_context_invalid' }),
    };
  }
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ sessions: [], seen_context: ctx }),
  };
};
""" + "(0, eval)(" + json.dumps(module) + ");\n" + \
        "globalThis.api = (0, eval)('(' + " + json.dumps(api_func) + " + ')');\n" + """
(async () => {
  const result = await api('/api/sessions', {timeoutMs: 0});
  console.log(JSON.stringify({
    result,
    requests: REQUESTS,
    token: sessionStorage.getItem('hermes-tab-profile-ctx'),
  }));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""
    payload = json.loads(_run_node(source))
    requests = payload["requests"]
    assert len(requests) == 3, f"expected refused → reissue → replay, got {requests}"
    assert "tab_context=STALE-BETA-TOKEN" in requests[0]
    assert "/api/profile/tab-context?profile=beta" in requests[1], \
        "the reissue must declare THIS tab's profile, not fall back to the cookie"
    assert "tab_context=STALE-BETA-TOKEN" not in requests[1], \
        "the reissue must not carry the token it is replacing"
    assert "tab_context=FRESH-TOKEN-FOR-beta" in requests[2]
    assert payload["result"]["seen_context"] == "FRESH-TOKEN-FOR-beta"
    assert payload["token"] == "FRESH-TOKEN-FOR-beta"


@nodeonly
def test_api_wrapper_surfaces_the_error_when_reissue_fails():
    """Fail closed: if a context cannot be reissued, the caller sees the error
    rather than a response silently served under the cookie profile."""
    module = _tab_context_module_source()
    api_func = _extract_func(WORKSPACE_JS, "api")
    source = """
const STORE = {'hermes-tab-profile-ctx': 'STALE', 'hermes-tab-profile-ctx-profile': 'beta'};
globalThis.sessionStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem(k, v) { STORE[k] = String(v); },
  removeItem(k) { delete STORE[k]; },
};
globalThis.location = { search: '', pathname: '/app/', href: 'https://example.test/app/' };
globalThis.document = { baseURI: 'https://example.test/app/' };
globalThis.window = { location: globalThis.location };
globalThis.showToast = () => {};
let attempts = 0;
globalThis.fetch = async (url) => {
  const parsed = new URL(url, 'https://example.test/app/');
  if (parsed.pathname.endsWith('/api/profile/tab-context')) return { ok: false, status: 500 };
  attempts += 1;
  return {
    ok: false, status: 409, statusText: 'Conflict',
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify({ error: 'tab_context_invalid' }),
  };
};
""" + "(0, eval)(" + json.dumps(module) + ");\n" + \
        "globalThis.api = (0, eval)('(' + " + json.dumps(api_func) + " + ')');\n" + """
(async () => {
  let status = null, message = null;
  try { await api('/api/sessions', {timeoutMs: 0}); }
  catch (e) { status = e.status; message = e.message; }
  console.log(JSON.stringify({status, message, attempts, token: sessionStorage.getItem('hermes-tab-profile-ctx')}));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""
    payload = json.loads(_run_node(source))
    assert payload["status"] == 409
    assert payload["message"] == "tab_context_invalid"
    assert payload["attempts"] == 1, "a failed reissue must not spin"
    assert payload["token"] is None, "the dead token must be gone regardless"


@nodeonly
def test_profile_link_never_carries_a_tab_context():
    """Defect 2a: a link naming profile B must not ship tab A's token."""
    profile_tab_url = _extract_func(SESSIONS_JS, "_profileTabUrl")
    source = """
globalThis.sessionStorage = {
  getItem(k) { return k === 'hermes-tab-profile-ctx' ? 'ALPHA-TOKEN' : null; },
};
globalThis.window = { location: { href: 'https://example.test/app/?keep=1&tab_context=ALPHA-TOKEN#frag' } };
globalThis.document = { baseURI: 'https://example.test/app/' };
""" + "const _profileTabUrl = (0, eval)('(' + " + json.dumps(profile_tab_url) + " + ')');\n" + """
console.log(JSON.stringify({ href: _profileTabUrl('beta') }));
"""
    href = json.loads(_run_node(source))["href"]
    assert "profile=beta" in href
    assert "keep=1" in href
    assert "#frag" in href
    assert "tab_context" not in href, f"profile links must not embed a tab context, got {href}"
    assert "ALPHA-TOKEN" not in href


@nodeonly
def test_tab_context_event_source_attaches_the_token_to_stream_urls():
    module = _tab_context_module_source()
    source = _node_env(search="", stored={"hermes-tab-profile-ctx": "LIVE-TOKEN"}) + """
const OPENED = [];
globalThis.EventSource = class { constructor(url, opts) { OPENED.push({url, opts}); } };
""" + "(0, eval)(" + json.dumps(module) + ");\n" + """
_tabContextEventSource('https://example.test/app/api/chat/stream?stream_id=abc', {withCredentials: true});
_tabContextEventSource('api/sessions/events');
console.log(JSON.stringify({opened: OPENED}));
"""
    opened = json.loads(_run_node(source))["opened"]
    assert "tab_context=LIVE-TOKEN" in opened[0]["url"]
    assert opened[0]["opts"] == {"withCredentials": True}
    assert "tab_context=LIVE-TOKEN" in opened[1]["url"]
    assert opened[1].get("opts") is None  # JSON.stringify drops an undefined value


# ── Defect 3: every EventSource routes through the tab-context helper ────────

def _event_source_call_sites() -> list[tuple[str, int, str]]:
    sites = []
    for path in sorted(STATIC_DIR.glob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "new EventSource(" in line:
                sites.append((path.name, lineno, line.strip()))
    return sites


def test_the_only_direct_event_source_construction_is_the_tab_context_helper():
    """Defect 3: a stream built with a bare `new EventSource(...)` carries no
    tab context and therefore resolves through the shared cookie."""
    sites = _event_source_call_sites()
    assert len(sites) == 1, f"EventSource must be constructed in exactly one place, found {sites}"
    filename, _lineno, line = sites[0]
    assert filename == "workspace.js"
    assert "_tabContextUrl(urlStr)" in line
    helper = _extract_func(WORKSPACE_JS, "_tabContextEventSource")
    assert "new EventSource(_tabContextUrl(urlStr),opts)" in helper


@pytest.mark.parametrize(
    "source_name,needle",
    [
        # The in-turn and reconnect chat-stream paths…
        ("messages.js", "api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}"),
        ("messages.js", "api/chat/stream?stream_id=${encodeURIComponent(streamId)}${replayParams}"),
        # …the persistent session stream…
        ("messages.js", "api/session/stream?session_id="),
        # …and the /btw ephemeral stream that round 1 missed (defect 3).
        ("messages.js", "api/chat/stream?stream_id='+encodeURIComponent(streamId)"),
        ("sessions.js", "api/sessions/events"),
        ("sessions.js", "api/sessions/gateway/stream"),
        ("panels.js", "/api/kanban/events/stream"),
        ("terminal.js", "api/terminal/output"),
    ],
)
def test_every_sse_stream_url_is_built_through_the_tab_context_helper(source_name, needle):
    """Each named stream endpoint feeds a `_tabContextEventSource(...)` call.

    Some call sites build the URL a few lines above the construction, so the
    check is windowed rather than single-line.
    """
    lines = (STATIC_DIR / source_name).read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert hits, f"{needle} not found in {source_name}"
    routed = False
    for i in hits:
        window = "\n".join(lines[max(0, i - 2):i + 6])
        if "_tabContextEventSource(" in window:
            routed = True
        assert "new EventSource(" not in window, \
            f"{source_name}: {needle} is opened with a bare EventSource"
    assert routed, f"{source_name}: {needle} never reaches _tabContextEventSource"


def test_both_chat_stream_reconnect_paths_go_through_the_helper():
    """The reconnect probes and the /btw stream all reopen with a tab context."""
    reconnect_sites = [
        line.strip()
        for line in MESSAGES_JS.splitlines()
        if "_wireSSE(" in line and "EventSource" in line
    ]
    assert len(reconnect_sites) >= 2, f"expected both reconnect paths, got {reconnect_sites}"
    for line in reconnect_sites:
        assert line.startswith("_wireSSE(_tabContextEventSource(") or "_wireSSE(_tabContextEventSource(" in line

    btw = _extract_func(MESSAGES_JS, "attachBtwStream")
    assert "_tabContextEventSource(" in btw
    assert "new EventSource(" not in btw


# ── SSE reconnect recovery + boot ordering, at the source level ──────────────

def test_sse_reconnect_paths_revalidate_the_tab_context_before_reopening():
    """EventSource cannot read a 409 body, so each reconnect path has to
    reissue before reopening or it loops against the same dead token."""
    for src, name in ((SESSIONS_JS, "sessions.js"), (MESSAGES_JS, "messages.js"),
                      (PANELS_JS, "panels.js")):
        assert "_revalidateTabContextAfterSseError()" in src, \
            f"{name} reconnects without revalidating the tab context"
    assert "_revalidateTabContextAfterSseError" in (STATIC_DIR / "terminal.js").read_text(encoding="utf-8")
    # …and must act on its verdict. A revalidation that could not restore a
    # context returns false, and reopening anyway reconnects through the
    # shared cookie (round 3, blocker 2).
    for src, name, reopen in (
        (SESSIONS_JS, "sessions.js", "ensureSessionEventsSSE()"),
        (SESSIONS_JS, "sessions.js", "probeGatewaySSEStatus()"),
        (MESSAGES_JS, "messages.js", "startSessionStream(sid)"),
        (PANELS_JS, "panels.js", "_kanbanStartEventStream()"),
    ):
        anchor = src.index("_revalidateTabContextAfterSseError().then(")
        while anchor >= 0 and reopen not in src[anchor:anchor + 700]:
            anchor = src.find("_revalidateTabContextAfterSseError().then(", anchor + 1)
        assert anchor > 0, f"{name}: {reopen} does not follow a revalidation"
        window = src[anchor:anchor + 700]
        assert "hasContext" in window or "hadContext" in window, \
            f"{name}: {reopen} reopens without checking the revalidation verdict"


def test_boot_awaits_a_bound_tab_context_before_profile_sensitive_requests():
    """Boot ordering (defect 2b): the acquisition is awaited and profile-bound,
    not a detached IIFE that early-returns on any inherited token."""
    assert "_acquireTabContext" not in BOOT_JS, "the fire-and-forget acquisition must be gone"
    ensure_pos = BOOT_JS.index("await _ensureTabContextForBoot(S.activeProfile||'default')")
    switch_pos = BOOT_JS.index("_profileSwitchCompleted=await switchToProfile(profileIntent.name)===true;")
    reasoning_pos = BOOT_JS.index(
        "if(typeof fetchReasoningChip==='function'&&(!_profileSwitchCompleted||!_profileSwitchChangedProfile)) fetchReasoningChip();"
    )
    assert switch_pos < ensure_pos < reasoning_pos, \
        "the context must be bound after the profile switch and before later boot fetches"


def test_in_place_profile_switch_rebinds_the_tab_context():
    """A tab that switches profile in place must rebind: the old token still
    resolves to the old profile and outranks the cookie."""
    switch_func = _extract_func(PANELS_JS, "switchToProfile")
    assert "await _rebindTabContext(S.activeProfile)" in switch_func
    assign_pos = switch_func.index("S.activeProfile = data.active || name;")
    rebind_pos = switch_func.index("await _rebindTabContext(S.activeProfile)")
    assert assign_pos < rebind_pos


# ── Round 3, blocker 1: boot binds BEFORE the first profile-sensitive request ─
#
# The tab-context helpers were correct in isolation while boot still called
# /api/settings and /api/profile/active before it parsed ?profile= and bound the
# context. A tab opened from profile A as "?profile=B" therefore asked the
# server two profile-scoped questions with no token attached, and the server
# answered both from the browser-wide cookie — as A. These tests compose the
# REAL boot scripts in index.html's order in a real browser and watch the wire.

APP_SCRIPTS = (
    "i18n.js", "icons.js", "assistant_turn_anchors.js", "ui.js", "workspace.js",
    "terminal.js", "sessions.js", "commands.js", "messages.js",
    "extension_settings.js", "panels.js", "onboarding.js", "boot.js", "outline.js",
)
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
BOOT_HARNESS_HTML = re.sub(r"<script\b[^>]*>.*?</script>", "", INDEX_HTML, flags=re.I | re.S)
BOOT_HARNESS_HTML = re.sub(r"<link\b[^>]*>", "", BOOT_HARNESS_HTML, flags=re.I)

# The fake server. It resolves every request the way api/profiles.py does —
# tab context first, browser-wide cookie second — and records what each request
# would have run as, which is the only thing these tests assert on.
_BOOT_NETWORK_STUB = """
window.__REQUESTS__ = [];
window.__TOKENS__ = Object.create(null);
window.__ISSUE_FAILS__ = %(issue_fails)s;
const COOKIE_PROFILE = 'alpha';
const _reply = (body, status) => ({
  ok: !status || status < 400,
  status: status || 200,
  statusText: '',
  redirected: false,
  headers: {get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null)},
  json: async () => body,
  text: async () => JSON.stringify(body),
  blob: async () => body,
  arrayBuffer: async () => body,
  clone() { return this; },
});
window.fetch = async (input, init) => {
  const raw = String((input && input.url) || input);
  const url = new URL(raw, location.href);
  const token = url.searchParams.get('tab_context');
  const path = url.pathname.replace(/^.*(\\/api\\/|\\/health)/, '$1');
  const issuing = path === '/api/profile/tab-context';
  // How the server would resolve this request's profile.
  const resolved = token
    ? (window.__TOKENS__[token] || null)
    : COOKIE_PROFILE;
  window.__REQUESTS__.push({
    path, token, resolved, issuing,
    method: ((init && init.method) || 'GET').toUpperCase(),
  });
  if (issuing) {
    if (window.__ISSUE_FAILS__) return _reply({error: 'boom'}, 500);
    const declared = url.searchParams.get('profile');
    const bound = declared || COOKIE_PROFILE;
    const issued = 'TOKEN-FOR-' + bound;
    window.__TOKENS__[issued] = bound;
    return _reply({token: issued, profile: bound, active_profile: COOKIE_PROFILE});
  }
  if (path === '/api/settings') return _reply({send_key: 'enter'});
  if (path === '/api/profile/active') {
    return _reply({name: resolved || 'default', is_default: false, path: '/tmp/hermes', default_workspace: null});
  }
  if (path === '/api/models') return _reply({models: [], groups: [], extra_models: [], aliases: {}});
  if (path === '/api/sessions') return _reply({sessions: []});
  return _reply({});
};
window.EventSource = class {
  constructor(url) { this.url = String(url); this.readyState = 1; }
  addEventListener() {} removeEventListener() {} close() { this.readyState = 2; }
};
"""

PROFILE_SENSITIVE_PATHS = ("/api/settings", "/api/profile/active", "/api/sessions", "/api/models")


@pytest.fixture(scope="module")
def boot_browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield browser
        browser.close()


def _boot_tab(browser, *, search: str, inherited: dict[str, str], issue_fails: bool = False):
    """Boot the real app scripts in a real browser and return the wire log."""
    page = browser.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html" if route.request.url.startswith("http://boot.test/") and "/static/" not in route.request.url else "text/plain",
            body=BOOT_HARNESS_HTML if route.request.url.startswith("http://boot.test/") and "/static/" not in route.request.url else "",
        ),
    )
    seed = ";".join(
        f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(v)})" for k, v in inherited.items()
    )
    # A new browsing context can inherit a COPY of the opener's sessionStorage;
    # seed it before any page script runs, exactly as the browser would.
    page.add_init_script(f"try {{ {seed} }} catch (e) {{}}" if seed else "0;")
    page.add_init_script(_BOOT_NETWORK_STUB % {"issue_fails": "true" if issue_fails else "false"})
    try:
        page.goto(f"http://boot.test/{search}", wait_until="domcontentloaded")
        for name in APP_SCRIPTS:
            page.add_script_tag(content=(STATIC_DIR / name).read_text(encoding="utf-8"))
        try:
            page.wait_for_function(
                "() => window.__REQUESTS__.some(r => r.path === '/api/profile/active')"
                " || (document.body.textContent || '').includes('Could not start this tab')",
                timeout=10000,
            )
        except Exception:
            pass
        page.wait_for_timeout(250)
        return page.evaluate("() => ({requests: window.__REQUESTS__, body: document.body.textContent || ''})")
    finally:
        page.close()


_INHERITED_OPENER_CONTEXT = {
    "hermes-tab-profile-ctx": "INHERITED-ALPHA-TOKEN",
    "hermes-tab-profile-ctx-profile": "alpha",
}


@pytest.mark.parametrize(
    "search,inherited,expected_profile",
    [
        # A tab opened from profile alpha's tab, naming beta, with a COPY of the
        # opener's sessionStorage — the new-tab case this feature exists for.
        ("?profile=beta", _INHERITED_OPENER_CONTEXT, "beta"),
        # An ordinary tab: no target, no inherited token. It must bind too, or
        # its first requests are the ones resolved by the shared cookie.
        ("", {}, "alpha"),
    ],
)
def test_boot_binds_a_tab_context_before_any_profile_sensitive_request(
    boot_browser, search, inherited, expected_profile,
):
    """Blocker 1: nothing profile-sensitive may precede the binding.

    With ?profile=beta the tab must be bound to BETA before it asks anything —
    the opener's cookie says alpha, and an unbound request is answered as
    alpha. Without the parameter the tab still binds (to the cookie-resolved
    profile) before its first request, so no request is ever resolved by the
    shared cookie except the issuance itself.
    """
    result = _boot_tab(boot_browser, search=search, inherited=inherited)
    requests = result["requests"]
    assert requests, "boot issued no requests at all"
    assert requests[0]["issuing"], \
        f"the first request must be the context issuance, got {requests[0]['path']}"

    sensitive = [r for r in requests if r["path"] in PROFILE_SENSITIVE_PATHS]
    assert any(r["path"] == "/api/settings" for r in sensitive), "boot never loaded settings"
    assert any(r["path"] == "/api/profile/active" for r in sensitive), "boot never resolved the profile"
    for request in sensitive:
        assert request["token"] == f"TOKEN-FOR-{expected_profile}", \
            f"{request['path']} carried {request['token']!r}, not this tab's context"
        assert request["resolved"] == expected_profile, \
            f"{request['path']} would have run as {request['resolved']!r}"

    for request in requests:
        assert request["token"] != "INHERITED-ALPHA-TOKEN", \
            "the opener's inherited token reached the wire"


def test_boot_stops_instead_of_falling_back_to_the_cookie_when_binding_fails(boot_browser):
    """Blocker 1, fail-closed half: no context, no profile-sensitive request.

    If the tab cannot be bound to the profile its URL names, running anyway
    means running the whole page as whoever the shared cookie names — behind a
    URL that says otherwise. Boot stops instead.
    """
    result = _boot_tab(
        boot_browser,
        search="?profile=beta",
        inherited={
            "hermes-tab-profile-ctx": "INHERITED-ALPHA-TOKEN",
            "hermes-tab-profile-ctx-profile": "alpha",
        },
        issue_fails=True,
    )
    requests = result["requests"]
    assert requests and all(r["issuing"] for r in requests), \
        f"boot kept talking to the server without a context: {[r['path'] for r in requests]}"
    assert not [r for r in requests if r["path"] in PROFILE_SENSITIVE_PATHS]
    assert "Could not start this tab" in result["body"]
