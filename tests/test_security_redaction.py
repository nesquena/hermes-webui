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
import pathlib
import sys
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


_needs_server = pytest.mark.skipif(
    not _server_is_up(),
    reason="Test server not running on port 8788 — run start.sh first",
)

BASE = "http://127.0.0.1:8788"

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


@_needs_server
def test_api_session_redacts_messages(test_server):
    """GET /api/session must not return credentials in messages or tool_calls."""
    sid = _create_session_with_credentials()
    data = _get(f"/api/session?session_id={sid}")
    dump = json.dumps(data)
    _assert_no_plaintext_credentials(dump, "GET /api/session messages")
    assert data["session"]["session_id"] == sid
    assert len(data["session"]["messages"]) == 3


@_needs_server
def test_api_session_redacts_title(test_server):
    """GET /api/session must not return a title containing a credential."""
    sid = _create_session_with_credentials()
    data = _get(f"/api/session?session_id={sid}")
    assert _FAKE_GITHUB_PAT not in data["session"]["title"]


@_needs_server
def test_api_sessions_list_redacts_titles(test_server):
    """GET /api/sessions must not return session titles containing credentials."""
    _create_session_with_credentials()
    data = _get("/api/sessions")
    dump = json.dumps(data)
    _assert_no_plaintext_credentials(dump, "GET /api/sessions titles")


@_needs_server
def test_api_session_export_redacts(test_server):
    """GET /api/session/export must not include credentials in the download."""
    sid = _create_session_with_credentials()
    raw_bytes = _get_raw(f"/api/session/export?session_id={sid}")
    text = raw_bytes.decode("utf-8")
    _assert_no_plaintext_credentials(text, "GET /api/session/export")


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
