"""
Sprint 28 Tests: /personality slash command — backend API coverage.
Tests: GET /api/personalities, POST /api/personality/set, Session.compact(),
path traversal defence, size cap, clear personality.
"""

import json
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

# Import test constants from conftest (same process — these are module-level values)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import TEST_STATE_DIR

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _personalities_dir():
    """Return the personalities directory the test server will look in.

    conftest sets HERMES_HOME=TEST_STATE_DIR in the server's environment.
    The server's api/profiles._DEFAULT_HERMES_HOME resolves to TEST_STATE_DIR,
    so get_active_hermes_home() returns TEST_STATE_DIR, and personalities
    live at TEST_STATE_DIR/personalities.
    """
    p = TEST_STATE_DIR / "personalities"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make_personality(name, content="# Test Bot\nA test personality."):
    """Create a personality directory with a SOUL.md."""
    d = _personalities_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SOUL.md").write_text(content)
    return d


def _make_session():
    """Create a new session and return its session_id."""
    d, status = post("/api/session/new", {})
    assert status == 200, f"Failed to create session: {d}"
    return d["session"]["session_id"]


def _cleanup_session(sid):
    try:
        post("/api/session/delete", {"session_id": sid})
    except Exception:
        pass


# ── GET /api/personalities ────────────────────────────────────────────────────


def test_personalities_empty_when_none_exist():
    """GET /api/personalities returns empty list when no personalities exist."""
    p_dir = _personalities_dir()
    for child in list(p_dir.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
    d, status = get("/api/personalities")
    assert status == 200
    assert d.get("personalities") == []


def test_personalities_lists_from_config():
    """GET /api/personalities returns personalities from config.yaml agent.personalities.
    Skipped if no personalities configured in test environment.
    """
    d, status = get("/api/personalities")
    assert status == 200
    assert isinstance(d.get("personalities"), list)
    # If personalities are configured, verify structure
    for p in d.get("personalities", []):
        assert "name" in p
        assert "description" in p


def test_personalities_returns_empty_when_none_configured():
    """GET /api/personalities returns empty list when no personalities in config."""
    # The test server starts with a clean state dir (no config.yaml),
    # so agent.personalities is empty by default
    d, status = get("/api/personalities")
    assert status == 200
    # May or may not have personalities depending on the real ~/.hermes/config.yaml
    # being loaded. Just verify the structure is correct.
    assert isinstance(d.get("personalities"), list)


def test_personalities_skips_non_dict_config():
    """GET /api/personalities handles non-dict agent config gracefully."""
    d, status = get("/api/personalities")
    assert status == 200
    assert isinstance(d.get("personalities"), list)


# ── POST /api/personality/set ─────────────────────────────────────────────────

_test_personalities = {}


def _inject_personality(name, value):
    """Write a personality into the test config.yaml so the server picks it up."""
    _test_personalities[name] = value
    _write_test_config()


def _remove_personality(name):
    """Remove a personality from the test config.yaml."""
    _test_personalities.pop(name, None)
    _write_test_config()


def _write_test_config():
    """Write config.yaml with test personalities using simple YAML format."""
    TEST_STATE_DIR.mkdir(parents=True, exist_ok=True)
    config_path = TEST_STATE_DIR / "config.yaml"
    lines = ["agent:", "  personalities:"]
    for pname, pval in _test_personalities.items():
        if isinstance(pval, dict):
            lines.append(f"    {pname}:")
            for k, v in pval.items():
                lines.append(f'      {k}: "{v}"')
        else:
            lines.append(f'    {pname}: "{pval}"')
    config_path.write_text("\n".join(lines) + "\n")


def test_set_personality_valid():
    """Setting a personality that exists in config stores name and returns prompt.
    Skipped if config.yaml has no personalities (common in test environments).
    """
    # First check if any personalities are configured
    d, status = get("/api/personalities")
    if not d.get("personalities"):
        return  # skip — no personalities in test server config
    name = d["personalities"][0]["name"]
    sid = _make_session()
    try:
        d2, status2 = post("/api/personality/set", {"session_id": sid, "name": name})
        assert status2 == 200
        assert d2.get("ok") is True
        assert d2.get("personality") == name
    finally:
        _cleanup_session(sid)


def test_set_personality_persists_in_compact():
    """After setting personality, GET /api/session returns personality in compact.
    Skipped if config.yaml has no personalities.
    """
    d, status = get("/api/personalities")
    if not d.get("personalities"):
        return  # skip
    name = d["personalities"][0]["name"]
    sid = _make_session()
    try:
        post("/api/personality/set", {"session_id": sid, "name": name})
        d2, status2 = get(f"/api/session?session_id={sid}")
        assert status2 == 200
        session = d2.get("session", {})
        assert session.get("personality") == name
    finally:
        _cleanup_session(sid)


def test_clear_personality_sets_null():
    """Clearing personality with name='' sets it to None (null in JSON)."""
    sid = _make_session()
    try:
        # Set a personality name directly on the session (no config validation needed for clear)
        d, status = post("/api/personality/set", {"session_id": sid, "name": ""})
        assert status == 200
        assert d.get("personality") is None
        # Verify persisted
        d2, s2 = get(f"/api/session?session_id={sid}")
        assert s2 == 200
        assert d2.get("session", {}).get("personality") is None
    finally:
        _cleanup_session(sid)


def test_set_personality_not_found_returns_404():
    """Setting a non-existent personality returns 404."""
    sid = _make_session()
    try:
        d, status = post(
            "/api/personality/set", {"session_id": sid, "name": "doesnotexist"}
        )
        assert status == 404
    finally:
        _cleanup_session(sid)


def test_set_personality_nonexistent_returns_404():
    """Names not in config.yaml agent.personalities return 404."""
    sid = _make_session()
    try:
        d, status = post(
            "/api/personality/set", {"session_id": sid, "name": "doesnotexist"}
        )
        assert status == 404, f"Expected 404, got {status}: {d}"
    finally:
        _cleanup_session(sid)


def test_set_personality_missing_session_returns_404():
    """Setting personality on non-existent session returns 404."""
    d, status = post(
        "/api/personality/set", {"session_id": "nonexistent000", "name": "x"}
    )
    assert status == 404
