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

BASE = "http://127.0.0.1:8788"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
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
    p = TEST_STATE_DIR / 'personalities'
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


def test_personalities_lists_valid_personalities():
    """GET /api/personalities returns personalities that have SOUL.md."""
    _make_personality("testbot", "# TestBot\nA helpful assistant.")
    try:
        d, status = get("/api/personalities")
        assert status == 200
        names = [p["name"] for p in d["personalities"]]
        assert "testbot" in names
        testbot = next(p for p in d["personalities"] if p["name"] == "testbot")
        assert testbot["description"] == "TestBot"
    finally:
        shutil.rmtree(_personalities_dir() / "testbot", ignore_errors=True)


def test_personalities_skips_dirs_without_soul_md():
    """Directories without SOUL.md are not listed."""
    empty_dir = _personalities_dir() / "nodoc"
    empty_dir.mkdir(exist_ok=True)
    try:
        d, status = get("/api/personalities")
        assert status == 200
        names = [p["name"] for p in d["personalities"]]
        assert "nodoc" not in names
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)


def test_personalities_skips_symlinks():
    """Symlinks inside personalities dir are skipped (security guard)."""
    p_dir = _personalities_dir()
    real_dir = p_dir.parent / "real_personality_target"
    real_dir.mkdir(exist_ok=True)
    (real_dir / "SOUL.md").write_text("# Leaked\nContent")
    link = p_dir / "symlinked"
    try:
        link.symlink_to(real_dir)
        d, status = get("/api/personalities")
        assert status == 200
        names = [p["name"] for p in d["personalities"]]
        assert "symlinked" not in names
    finally:
        link.unlink(missing_ok=True)
        shutil.rmtree(real_dir, ignore_errors=True)


# ── POST /api/personality/set ─────────────────────────────────────────────────

def test_set_personality_valid():
    """Setting a valid personality stores name and returns prompt."""
    _make_personality("assistant", "# Assistant\nBe helpful.")
    sid = _make_session()
    try:
        d, status = post("/api/personality/set", {"session_id": sid, "name": "assistant"})
        assert status == 200
        assert d.get("ok") is True
        assert d.get("personality") == "assistant"
        assert "Assistant" in d.get("prompt", "")
    finally:
        _cleanup_session(sid)
        shutil.rmtree(_personalities_dir() / "assistant", ignore_errors=True)


def test_set_personality_persists_in_compact():
    """After setting personality, GET /api/session returns personality in compact."""
    _make_personality("coder", "# Coder\nWrite clean code.")
    sid = _make_session()
    try:
        post("/api/personality/set", {"session_id": sid, "name": "coder"})
        d, status = get(f"/api/session?session_id={sid}")
        assert status == 200
        session = d.get("session", {})
        assert session.get("personality") == "coder"
    finally:
        _cleanup_session(sid)
        shutil.rmtree(_personalities_dir() / "coder", ignore_errors=True)


def test_clear_personality_sets_null():
    """Clearing personality with name='' sets it to None (null in JSON)."""
    _make_personality("pirate", "# Pirate\nArrr.")
    sid = _make_session()
    try:
        post("/api/personality/set", {"session_id": sid, "name": "pirate"})
        d, status = post("/api/personality/set", {"session_id": sid, "name": ""})
        assert status == 200
        assert d.get("personality") is None
        # Verify persisted via direct session fetch
        d2, s2 = get(f"/api/session?session_id={sid}")
        assert s2 == 200
        assert d2.get("session", {}).get("personality") is None
    finally:
        _cleanup_session(sid)
        shutil.rmtree(_personalities_dir() / "pirate", ignore_errors=True)


def test_set_personality_not_found_returns_404():
    """Setting a non-existent personality returns 404."""
    sid = _make_session()
    try:
        d, status = post("/api/personality/set",
                         {"session_id": sid, "name": "doesnotexist"})
        assert status == 404
    finally:
        _cleanup_session(sid)


def test_set_personality_path_traversal_rejected():
    """Personality names with path traversal chars are rejected (400)."""
    sid = _make_session()
    try:
        for bad_name in ["../etc", "a/b", ".hidden", "has space"]:
            d, status = post("/api/personality/set",
                             {"session_id": sid, "name": bad_name})
            assert status == 400, (
                f"Expected 400 for name={bad_name!r}, got {status}: {d}"
            )
    finally:
        _cleanup_session(sid)


def test_set_personality_missing_session_returns_404():
    """Setting personality on non-existent session returns 404."""
    _make_personality("x", "# X\nTest.")
    try:
        d, status = post("/api/personality/set",
                         {"session_id": "nonexistent000", "name": "x"})
        assert status == 404
    finally:
        shutil.rmtree(_personalities_dir() / "x", ignore_errors=True)


def test_set_personality_size_cap():
    """SOUL.md files larger than MAX_FILE_BYTES are rejected."""
    from api.config import MAX_FILE_BYTES
    big_content = "A" * (MAX_FILE_BYTES + 1)
    _make_personality("toobig", big_content)
    sid = _make_session()
    try:
        d, status = post("/api/personality/set", {"session_id": sid, "name": "toobig"})
        assert status == 400
        assert "exceeds" in d.get("error", "").lower()
    finally:
        _cleanup_session(sid)
        shutil.rmtree(_personalities_dir() / "toobig", ignore_errors=True)
