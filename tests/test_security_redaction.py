"""
Security tests: credential redaction in API responses.

Verifies that credentials (GitHub PATs, API keys, etc.) are masked in:
  - GET /api/session  (messages and tool_calls)
  - GET /api/memory   (MEMORY.md and USER.md content)
  - GET /api/session/export (downloaded JSON)
  - SSE done event    (session payload in stream)

Tests run against the isolated test test_server on port 8788.
"""

import json
import importlib
import pathlib
import sys
import types
import urllib.request
import urllib.error
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))


def _server_is_up(port: int = 8788) -> bool:
    """Return True if the test server is accepting connections."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


# _needs_server: these tests require the conftest test_server fixture (port 8788).
# The skipif is evaluated lazily via the fixture, not at collection time.
_needs_server = pytest.mark.usefixtures("test_server")

from tests._pytest_port import BASE

# Sample credentials that should be masked in every API response
_FAKE_GITHUB_PAT = "ghp_TestFakeCredential1234567890ab"
_FAKE_SK_KEY     = "sk-TestFakeOpenAIKey1234567890abcdef"
_FAKE_HF_TOKEN   = "hf_TestFakeHuggingFaceToken12345"
_FAKE_AWS_KEY    = "AKIATESTFAKEKEY12345"


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _get_raw(path):
    """Return raw bytes (used for export endpoint)."""
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read()


def _assert_no_plaintext_credentials(text: str, label: str = ""):
    """Assert that none of the fake credential strings appear in text."""
    for cred in (_FAKE_GITHUB_PAT, _FAKE_SK_KEY, _FAKE_HF_TOKEN, _FAKE_AWS_KEY):
        assert cred not in text, (
            f"{label}: credential '{cred[:12]}...' found in plaintext. "
            "Redaction is not working."
        )


# ── helpers.py unit tests (import-level, no test_server needed) ───────────────────

def test_redact_value_str():
    """_redact_value masks a plaintext GitHub PAT in a string."""
    from api.helpers import _redact_value
    result = _redact_value(f"my token is {_FAKE_GITHUB_PAT} bye")
    assert _FAKE_GITHUB_PAT not in result
    assert "ghp_Te" in result  # prefix preserved


def test_redact_value_dict():
    """_redact_value recurses into dicts."""
    from api.helpers import _redact_value
    d = {"content": f"key={_FAKE_SK_KEY}", "role": "user"}
    result = _redact_value(d)
    assert _FAKE_SK_KEY not in result["content"]
    assert result["role"] == "user"  # innocent values untouched


def test_redact_value_list():
    """_redact_value recurses into lists."""
    from api.helpers import _redact_value
    lst = [{"content": _FAKE_GITHUB_PAT}, {"content": "safe text"}]
    result = _redact_value(lst)
    assert _FAKE_GITHUB_PAT not in result[0]["content"]
    assert result[1]["content"] == "safe text"


def test_redact_text_skips_full_redactor_for_safe_text(monkeypatch):
    """Large ordinary transcript text should not pay the full redactor pass."""
    import api.helpers as helpers

    calls = []
    monkeypatch.setattr(helpers, "_redact_fn_cached", lambda text: calls.append(text) or text)

    safe_text = "ordinary session transcript without credential markers\n" * 500
    assert helpers._redact_text(safe_text, _enabled=True) == safe_text
    assert calls == []


def test_redact_text_still_runs_full_redactor_for_sensitive_markers(monkeypatch):
    """The cheap prefilter must preserve the hard redaction boundary."""
    import api.helpers as helpers

    calls = []

    def fake_redactor(text):
        calls.append(text)
        return text.replace(_FAKE_SK_KEY, "sk-Tes...cdef")

    monkeypatch.setattr(helpers, "_redact_fn_cached", fake_redactor)

    result = helpers._redact_text(f"token={_FAKE_SK_KEY}", _enabled=True)

    assert _FAKE_SK_KEY not in result
    assert calls == [f"token={_FAKE_SK_KEY}"]


@pytest.mark.parametrize("prefix,suffix", [
    ("sk-", "TestCredential1234567890"),
    ("ghp_", "TestCredential1234567890"),
    ("github_pat_", "TestCredential_1234567890"),
    ("gho_", "TestCredential1234567890"),
    ("ghu_", "TestCredential1234567890"),
    ("ghs_", "TestCredential1234567890"),
    ("ghr_", "TestCredential1234567890"),
    ("xoxb-", "TestCredential1234567890"),
    ("xoxa-", "TestCredential1234567890"),
    ("xoxp-", "TestCredential1234567890"),
    ("xoxr-", "TestCredential1234567890"),
    ("xoxs-", "TestCredential1234567890"),
    ("AIza", "TestCredential1234567890abcdefghi"),
    ("pplx-", "TestCredential1234567890"),
    ("fal_", "TestCredential1234567890"),
    ("fc-", "TestCredential1234567890"),
    ("bb_live_", "TestCredential1234567890"),
    ("gAAAA", "TestCredential1234567890abcd"),
    ("AKIA", "TESTCREDENTIAL12"),
    ("sk_" + "live_", "TestCredential1234567890"),
    ("sk_" + "test_", "TestCredential1234567890"),
    ("rk_" + "live_", "TestCredential1234567890"),
    ("SG.", "TestCredential1234567890"),
    ("hf_", "TestCredential1234567890"),
    ("r8_", "TestCredential1234567890"),
    ("npm_", "TestCredential1234567890"),
    ("pypi-", "TestCredential1234567890"),
    ("dop_v1_", "TestCredential1234567890"),
    ("doo_v1_", "TestCredential1234567890"),
    ("am_", "TestCredential1234567890"),
    ("sk_", "TestCredential1234567890"),
    ("tvly-", "TestCredential1234567890"),
    ("exa_", "TestCredential1234567890"),
    ("gsk_", "TestCredential1234567890"),
    ("syt_", "TestCredential1234567890"),
    ("retaindb_", "TestCredential1234567890"),
    ("hsk-", "TestCredential1234567890"),
    ("mem0_", "TestCredential1234567890"),
    ("brv_", "TestCredential1234567890"),
])
def test_redact_text_prefilter_covers_known_prefixed_credentials(prefix, suffix):
    """Every known prefix must still reach the hard redactor."""
    import api.helpers as helpers

    token = prefix + suffix
    result = helpers._redact_text(f"credential={token}", _enabled=True)

    assert token not in result


@pytest.mark.parametrize("text", [
    # OAuth callback URL with `code=` query param
    "https://example.com/callback?code=AUTH_OPAQUE_VALUE",
    # URL userinfo (user:password embedded in scheme://)
    "https://admin:supersecretpassword@api.example.com/v1",
    # Signed-URL sensitive query param
    "https://cdn.example.com/file.zip?signature=ABCDEFGHIJKL",
    # Session-token query param
    "https://example.com/dashboard?session=xyzABC999DEF",
    # WebSocket URL with token query param
    "wss://example.com/ws?token=jwt_ABCDEFGHIJ",
    # FTP userinfo
    "ftp://user:pwd@files.example.com/path",
])
def test_redact_text_prefilter_routes_url_containing_strings_to_hard_redactor(text):
    """Stage-348 Opus follow-up to PR #2171: the credential prefilter must
    catch URL userinfo and sensitive query params so they still reach the
    hard agent redactor instead of bypassing it.

    Pre-fix, the prefilter only listed specific DB scheme prefixes
    (postgres://, mysql://, etc.) and a closed set of form keys, so OAuth
    callback URLs pasted into chat could pass through to the response
    verbatim. The fix adds the generic "://" marker so http(s)/ws(s)/ftp
    URLs always route to the hard redactor.

    We test the *prefilter routing decision* here — `_might_contain_sensitive_text`
    must return True for any URL-shaped string — rather than asserting on the
    specific output of the agent redactor (which varies between hermes-agent
    versions and CI vs local installs).
    """
    import api.helpers as helpers

    assert helpers._might_contain_sensitive_text(text) is True, (
        f"URL-shaped string {text!r} should route to hard redactor but the "
        f"prefilter rejected it. Pre-fix this allowed OAuth callback URLs, "
        f"URL userinfo, and signed-URL query params to bypass redaction."
    )


def test_redact_text_prefilter_admits_plain_text_without_url_or_credentials():
    """Stage-348 follow-up companion: plain text with no URL or credential
    marker still bypasses the hard redactor (the prefilter's whole purpose
    is to skip the expensive pass when no markers are present)."""
    import api.helpers as helpers

    assert helpers._might_contain_sensitive_text("Hi how are you today?") is False
    assert helpers._might_contain_sensitive_text("The user said 'hello'") is False
    assert helpers._might_contain_sensitive_text("") is False
    assert helpers._might_contain_sensitive_text(None) is False  # type: ignore[arg-type]


def test_redact_value_works_with_legacy_agent_redact_signature(monkeypatch):
    """_redact_text must tolerate older redact_sensitive_text(text) signatures."""
    fake_agent = types.ModuleType("agent")
    fake_redact = types.ModuleType("agent.redact")

    def _legacy_redact_sensitive_text(text):
        return text

    fake_redact.redact_sensitive_text = _legacy_redact_sensitive_text
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.redact", fake_redact)

    import api.helpers as helpers
    helpers = importlib.reload(helpers)
    try:
        result = helpers._redact_value(f"token={_FAKE_GITHUB_PAT}")
        assert _FAKE_GITHUB_PAT not in result
        assert "ghp_Te" in result
    finally:
        importlib.reload(helpers)


def test_redact_session_data_messages():
    """redact_session_data masks credentials in messages[]."""
    from api.helpers import redact_session_data
    session = {
        "session_id": "abc123",
        "title": f"my token {_FAKE_GITHUB_PAT}",
        "messages": [
            {"role": "user", "content": f"token: {_FAKE_GITHUB_PAT}"},
            {"role": "assistant", "content": "sure"},
        ],
        "tool_calls": [
            {"name": "terminal", "args": {"command": f"gh auth login --token {_FAKE_GITHUB_PAT}"},
             "snippet": "ok"},
        ],
    }
    result = redact_session_data(session)
    dump = json.dumps(result)
    _assert_no_plaintext_credentials(dump, "redact_session_data")
    # Safe fields remain intact
    assert result["session_id"] == "abc123"
    assert result["messages"][1]["content"] == "sure"


def test_redact_session_data_multiple_cred_types():
    """redact_session_data handles sk-, ghp_, hf_, and AKIA keys."""
    from api.helpers import redact_session_data
    session = {
        "title": "test",
        "messages": [{"role": "user", "content": (
            f"openai={_FAKE_SK_KEY} "
            f"github={_FAKE_GITHUB_PAT} "
            f"hf={_FAKE_HF_TOKEN} "
            f"aws={_FAKE_AWS_KEY}"
        )}],
        "tool_calls": [],
    }
    result = redact_session_data(session)
    dump = json.dumps(result)
    _assert_no_plaintext_credentials(dump, "multi-type redaction")


def test_redact_session_data_non_sensitive_unchanged():
    """redact_session_data does not corrupt innocent content."""
    from api.helpers import redact_session_data
    session = {
        "title": "Hello world",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "tool_calls": [{"name": "terminal", "snippet": "4"}],
    }
    result = redact_session_data(session)
    assert result["title"] == "Hello world"
    assert result["messages"][0]["content"] == "What is 2+2?"
    assert result["tool_calls"][0]["snippet"] == "4"


# ── API-level tests (require running test server started by conftest.py) ─────
# Run via `start.sh && pytest tests/test_security_redaction.py -v`

def _create_session_with_credentials() -> str:
    """Write a session file with credential-containing messages directly to disk.

    Bypasses the server's in-memory cache so the GET endpoint is forced to read
    from disk, exercising the redaction code path on load.
    Uses TEST_STATE_DIR from conftest.py (the isolated test server state directory).
    """
    import time, uuid
    try:
        from conftest import TEST_STATE_DIR
        sessions_dir = TEST_STATE_DIR / "sessions"
    except ImportError:
        from api.config import SESSION_DIR as sessions_dir
    sessions_dir = pathlib.Path(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Use a unique session ID that is NOT in the server's LRU cache
    sid = "sec_test_" + uuid.uuid4().hex[:8]
    now = time.time()
    session_file = sessions_dir / f"{sid}.json"
    session_file.write_text(json.dumps({
        "session_id": sid,
        "title": f"session with {_FAKE_GITHUB_PAT}",
        "workspace": "/tmp",
        "model": "test",
        "created_at": now,
        "updated_at": now,
        "pinned": False, "archived": False, "project_id": None,
        "profile": "default", "input_tokens": 0, "output_tokens": 0,
        "estimated_cost": None, "personality": None,
        "messages": [
            {"role": "user",      "content": f"my PAT is {_FAKE_GITHUB_PAT}"},
            {"role": "assistant", "content": f"sk key is {_FAKE_SK_KEY}"},
            {"role": "tool",      "content": "result ok", "name": "terminal"},
        ],
        "tool_calls": [
            {"name": "terminal",
             "args": {"command": f"gh auth login --token {_FAKE_GITHUB_PAT}"},
             "snippet": "blocked"}
        ],
    }))
    return sid


def test_api_session_redacts_messages():
    """GET /api/session route must call redact_session_data() before returning."""
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes.handle_get)
    # Verify redact_session_data is applied to the session payload
    assert "redact_session_data" in src, (
        "api/routes.py handle_get must call redact_session_data() on /api/session response"
    )


def test_api_session_redacts_title():
    """redact_session_data must redact credentials from session title field."""
    from api.helpers import redact_session_data
    session = {
        "session_id": "abc123",
        "title": f"session with {_FAKE_GITHUB_PAT}",
        "messages": [],
        "tool_calls": [],
    }
    result = redact_session_data(session)
    assert _FAKE_GITHUB_PAT not in result["title"], (
        f"redact_session_data must mask credentials in title field"
    )
    assert result["session_id"] == "abc123"  # safe fields preserved


@_needs_server
def test_api_sessions_list_redacts_titles(test_server):
    """GET /api/sessions must not return session titles containing credentials."""
    _create_session_with_credentials()
    data = _get("/api/sessions")
    dump = json.dumps(data)
    _assert_no_plaintext_credentials(dump, "GET /api/sessions titles")


def test_api_session_export_redacts():
    """GET /api/session/export must call redact_session_data() in _handle_session_export."""
    import inspect
    import api.routes as routes
    # The export handler is a separate function (_handle_session_export)
    src = inspect.getsource(routes._handle_session_export)
    assert "redact_session_data" in src, (
        "_handle_session_export must call redact_session_data() before serving download"
    )


@_needs_server
def test_api_memory_redacts_via_write_read(test_server):
    """Credential written to MEMORY.md must be masked in GET /api/memory response."""
    original = _get("/api/memory").get("memory", "")

    cred_content = f"GitHub PAT: {_FAKE_GITHUB_PAT}\nNormal note: hello world"
    data, status = _post("/api/memory/write", {"section": "memory", "content": cred_content})
    assert status == 200, f"memory/write failed: {data}"

    try:
        read_back = _get("/api/memory")
        dump = json.dumps(read_back)
        _assert_no_plaintext_credentials(dump, "GET /api/memory")
        assert "hello world" in read_back["memory"]   # non-sensitive content preserved
    finally:
        _post("/api/memory/write", {"section": "memory", "content": original})


# ── startup: fix_credential_permissions ──────────────────────────────────────

def test_fix_credential_permissions_corrects_loose_files(tmp_path, monkeypatch):
    """fix_credential_permissions() tightens group/other read bits."""
    import os
    from api.startup import fix_credential_permissions

    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=abc")
    env_file.chmod(0o644)  # world-readable -- should be fixed

    google_file = tmp_path / "google_token.json"
    google_file.write_text("{}")
    google_file.chmod(0o664)  # group-readable -- should be fixed

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fix_credential_permissions()

    import stat
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600, ".env not fixed to 600"
    assert stat.S_IMODE(google_file.stat().st_mode) == 0o600, "google_token.json not fixed to 600"


def test_fix_credential_permissions_skips_correct_files(tmp_path, monkeypatch):
    """fix_credential_permissions() does not alter already-strict files."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=abc")
    env_file.chmod(0o600)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from api.startup import fix_credential_permissions
    fix_credential_permissions()

    import stat
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
