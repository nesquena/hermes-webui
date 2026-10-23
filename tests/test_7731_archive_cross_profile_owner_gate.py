"""#7731: the archive cross-profile fallback must require a claimed, validated owner.

Reviewer gate (nesquena-hermes, re-gate of head 6da588e3) plus the greptile
findings on the same code path. Every test here drives the REAL route
(``routes.handle_post``) against REAL per-profile ``state.db`` files and the
REAL ``_lookup_cli_session_metadata`` / ``_resolve_cli_import_metadata`` /
``_normalize_import_profile_value`` helpers. The only seam stubbed is
``get_cli_sessions``'s Claude-Code directory scan, which is irrelevant here;
the agent-store projection itself runs for real against the temp databases.

Covered:
  1. BLOCKER — ``all_profiles:true`` with an ABSENT ``profile`` must be
     rejected, not materialized+archived.
  2. BLOCKER — ``all_profiles:true`` with an INVALID ``profile`` must be
     rejected the same way.
  3. SECONDARY — a wrong asserted owner must be a distinguishable 409
     ``session_profile_mismatch``, not a 404 "Session not found".
  4. The correct asserted owner still archives successfully (200), and the
     archived sidecar keeps its CLI source identity (greptile P1).
  5. An unqualified request from alpha still 404s and creates no sidecar.
  6. A previously archived foreign session can be restored through the same
     scoped request instead of being blocked by the visibility guard
     (greptile P1).
  7. Isolated-profile mode never widens the search, and the saved
     all-profiles toggle must not strand the pinned profile's own sessions
     (greptile P1).
"""

import collections
import io
import json
import sqlite3
from urllib.parse import urlparse

import pytest


# ---------------------------------------------------------------------------
# Real per-profile state.db harness
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    cwd TEXT,
    rewind_count INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp REAL,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_call_count INTEGER DEFAULT 0
);
"""


def _write_profile_db(db_path, rows):
    """Create a real agent state.db containing *rows* = [(sid, title, source)]."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    for sid, title, source in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
            "VALUES (?, ?, ?, ?, 1781024055.0, ?, ?)",
            (sid, source, "MiniMax-M3", 2, title, "/root"),
        )
        for i in range(2):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, "user" if i == 0 else "assistant", f"{sid} msg {i}", 1781024055.0 + i),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def two_profile_world(tmp_path, monkeypatch):
    """Real alpha (active) + bravo profiles, each with its own state.db.

    Active profile is ``alpha``; ``bravo`` holds the CLI-only session the
    reviewer's reproducer uses. The WebUI sidecar store is a fresh empty dir so
    "did this request publish a sidecar?" is directly observable on disk.
    """
    routes = pytest.importorskip("api.routes")
    models = pytest.importorskip("api.models")
    profiles = pytest.importorskip("api.profiles")

    home = tmp_path / "home"
    profiles_root = home / "profiles"
    (profiles_root / "alpha").mkdir(parents=True)
    (profiles_root / "bravo").mkdir(parents=True)

    _write_profile_db(profiles_root / "alpha" / "state.db", [])
    _write_profile_db(
        profiles_root / "bravo" / "state.db",
        [
            ("cli_bravo_only_0001", "Bravo private", "tui"),
            ("cli_bravo_only_0002", "Bravo private two", "tui"),
            ("cli_bravo_only_0003", "Bravo private three", "tui"),
            ("cli_bravo_only_0004", "Bravo private four", "tui"),
        ],
    )
    # alpha owns one session of its own, used to prove unqualified archives
    # of the ACTIVE profile still work.
    _write_profile_db(
        profiles_root / "alpha" / "state.db",
        [("cli_alpha_0001", "Alpha own session", "tui")],
    )

    sessions_dir = tmp_path / "webui-state" / "sessions"
    sessions_dir.mkdir(parents=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(profiles, "_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profiles_root / "alpha")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alpha"),
    )
    # routes/models imported these names at module load; rebind the aliases.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha", raising=False)
    monkeypatch.setattr(models, "_active_state_db_path",
                        lambda: profiles_root / "alpha" / "state.db")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(routes, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(routes, "SESSIONS", collections.OrderedDict())
    monkeypatch.setattr(models, "SESSIONS", collections.OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude-code"))
    # No delegated children / messaging rows in these fixtures.
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda s: {})

    models.clear_cli_sessions_cache()
    yield {
        "routes": routes,
        "models": models,
        "profiles_root": profiles_root,
        "sessions_dir": sessions_dir,
    }
    models.clear_cli_sessions_cache()


# ---------------------------------------------------------------------------
# Real request plumbing
# ---------------------------------------------------------------------------

class _RealPostHandler:
    """Feed a real JSON body through handle_post's read path."""

    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.response_headers = {}
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/api/session/archive"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _archive(world, body):
    """POST /api/session/archive through the real handle_post dispatch."""
    routes = world["routes"]
    handler = _RealPostHandler(body)
    routes.handle_post(handler, urlparse("/api/session/archive"))
    payload = {}
    raw = handler.wfile.getvalue()
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
    return handler.status, payload


def _sidecar_exists(world, sid):
    return (world["sessions_dir"] / f"{sid}.json").exists()


# ---------------------------------------------------------------------------
# 1. BLOCKER — all_profiles without a validated claimed owner
# ---------------------------------------------------------------------------

class TestAllProfilesRequiresClaimedOwner:
    def test_all_profiles_without_profile_is_rejected(self, two_profile_world):
        """all_profiles:true + NO profile must NOT resolve bravo's session.

        Before the fix this returned 200 and published a bravo-owned sidecar.
        """
        status, payload = _archive(
            two_profile_world,
            {"session_id": "cli_bravo_only_0001", "archived": True, "all_profiles": True},
        )
        assert status == 400, (
            "archive must reject all_profiles:true with no claimed profile, got "
            f"{status} {payload!r}"
        )
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0001"), (
            "a rejected cross-profile archive must not publish a sidecar"
        )

    def test_all_profiles_with_invalid_profile_is_rejected(self, two_profile_world):
        """all_profiles:true + a malformed profile must be rejected identically."""
        status, payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0002",
                "archived": True,
                "all_profiles": True,
                "profile": "not a valid profile!!",
            },
        )
        assert status == 400, (
            "archive must reject all_profiles:true with an invalid profile, got "
            f"{status} {payload!r}"
        )
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0002")

    def test_rejected_archive_does_not_flip_the_session_archived(self, two_profile_world):
        """The bravo row in state.db must stay unarchived after a rejection."""
        _archive(
            two_profile_world,
            {"session_id": "cli_bravo_only_0003", "archived": True, "all_profiles": True},
        )
        bravo_db = two_profile_world["profiles_root"] / "bravo" / "state.db"
        conn = sqlite3.connect(str(bravo_db))
        try:
            archived = conn.execute(
                "SELECT archived FROM sessions WHERE id = ?", ("cli_bravo_only_0003",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert not archived, "a rejected archive must not mutate the foreign session"


# ---------------------------------------------------------------------------
# 2. SECONDARY — a wrong asserted owner is a distinguishable 409
# ---------------------------------------------------------------------------

class TestWrongOwnerIsDistinguishable:
    def test_wrong_asserted_owner_returns_409_not_404(self, two_profile_world):
        """profile:'alpha' for a bravo-only session must be a 409 mismatch.

        The real resolver prefilters a mismatched owner to ``{}``, so the
        handler used to 404 before its own 409 check could run and the caller
        could not tell a profile conflict from a missing session.
        """
        status, payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0001",
                "archived": True,
                "all_profiles": True,
                "profile": "alpha",
            },
        )
        assert status == 409, (
            "a wrong asserted owner must be 409 session_profile_mismatch, got "
            f"{status} {payload!r}"
        )
        assert payload.get("code") == "session_profile_mismatch"
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0001"), (
            "a 409 conflict must not publish a sidecar"
        )

    def test_profile_claim_without_all_profiles_keeps_the_404(self, two_profile_world):
        """Maintainer fold (senior re-gate): ``profile`` without ``all_profiles`` is an active-profile archive.

        It used to trigger the all-profiles owner probe and answer 409 naming the owner; master and the documented
        contract answer 404 because the CLI lookup stays active-profile-only for unqualified requests.
        """
        status, payload = _archive(
            two_profile_world,
            {"session_id": "cli_bravo_only_0001", "archived": True, "profile": "alpha"},
        )
        assert status == 404, f"expected 404 for an active-profile archive of a foreign session, got {status} {payload!r}"
        assert payload.get("profile") != "bravo", "an active-profile request must not name the owning profile"
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0001")

    def test_unknown_session_with_valid_scoped_request_is_404(self, two_profile_world):
        """A genuinely absent session keeps the 404 contract."""
        status, payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_no_such_session_9999",
                "archived": True,
                "all_profiles": True,
                "profile": "bravo",
            },
        )
        assert status == 404, f"expected 404 for an unknown session, got {status} {payload!r}"


# ---------------------------------------------------------------------------
# 3. The correct owner still works, and keeps CLI source identity
# ---------------------------------------------------------------------------

class TestCorrectOwnerArchives:
    def test_correct_owner_archives_and_keeps_cli_source(self, two_profile_world):
        """all_profiles:true + profile:'bravo' archives bravo's session (200)."""
        status, payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0003",
                "archived": True,
                "all_profiles": True,
                "profile": "bravo",
            },
        )
        assert status == 200, f"expected 200 for the correct owner, got {status} {payload!r}"
        assert payload.get("ok") is True
        sidecar = json.loads(
            (two_profile_world["sessions_dir"] / "cli_bravo_only_0003.json").read_text()
        )
        assert sidecar.get("archived") is True
        assert sidecar.get("profile") == "bravo"
        # greptile P1: the reload must not persist a WebUI-native copy.
        assert sidecar.get("is_cli_session") is True, (
            "archived CLI session lost is_cli_session — the resolved cli_meta was "
            "not re-applied after the lock-held reload"
        )
        assert sidecar.get("source_tag") or sidecar.get("raw_source"), (
            "archived CLI session lost its source identity fields"
        )

    def test_unqualified_archive_of_foreign_session_is_404_without_sidecar(self, two_profile_world):
        """No all_profiles flag at all → bravo's session stays untouched."""
        status, payload = _archive(
            two_profile_world,
            {"session_id": "cli_bravo_only_0004", "archived": True},
        )
        assert status == 404, f"expected 404 for an unqualified foreign archive, got {status} {payload!r}"
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0004")

    def test_unqualified_archive_of_own_session_still_succeeds(self, two_profile_world):
        """The active profile's own CLI session still archives without flags."""
        status, payload = _archive(
            two_profile_world,
            {"session_id": "cli_alpha_0001", "archived": True},
        )
        assert status == 200, f"unqualified own-profile archive regressed: {status} {payload!r}"


# ---------------------------------------------------------------------------
# 4. greptile P1 — archived foreign sessions must be restorable
# ---------------------------------------------------------------------------

class TestArchivedForeignSessionIsRestorable:
    def test_scoped_unarchive_succeeds_after_archive(self, two_profile_world):
        """Archiving a foreign session must not brick its own restore.

        The archive writes a bravo-owned sidecar; the next request hits
        _guard_request_session_visibility, which used to answer 409 before the
        archive handler ran — even with a correct all_profiles + profile.
        """
        archive_status, _ = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0003",
                "archived": True,
                "all_profiles": True,
                "profile": "bravo",
            },
        )
        assert archive_status == 200

        restore_status, restore_payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0003",
                "archived": False,
                "all_profiles": True,
                "profile": "bravo",
            },
        )
        assert restore_status == 200, (
            "an archived foreign session must be restorable through the same "
            f"scoped request, got {restore_status} {restore_payload!r}"
        )
        sidecar = json.loads(
            (two_profile_world["sessions_dir"] / "cli_bravo_only_0003.json").read_text()
        )
        assert sidecar.get("archived") is False


# ---------------------------------------------------------------------------
# 5. greptile P1 — isolated-profile mode
# ---------------------------------------------------------------------------

class TestIsolatedProfileMode:
    def test_isolated_mode_rejects_all_profiles_archive(self, two_profile_world, monkeypatch):
        routes = two_profile_world["routes"]
        monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
        status, _ = _archive(
            two_profile_world,
            {
                "session_id": "cli_bravo_only_0001",
                "archived": True,
                "all_profiles": True,
                "profile": "bravo",
            },
        )
        assert status == 403, f"isolated mode must refuse a cross-profile archive, got {status}"
        assert not _sidecar_exists(two_profile_world, "cli_bravo_only_0001")

    def test_isolated_mode_still_archives_the_pinned_profile_own_session(
        self, two_profile_world, monkeypatch
    ):
        """A saved all-profiles toggle must not strand the pinned profile's sessions.

        The browser can restore _showAllProfiles=true while the server runs in
        isolated mode; the session it targets belongs to the PINNED profile, so
        the archive must be allowed to proceed on the owner match.
        """
        routes = two_profile_world["routes"]
        monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
        status, payload = _archive(
            two_profile_world,
            {
                "session_id": "cli_alpha_0001",
                "archived": True,
                "all_profiles": True,
                "profile": "alpha",
            },
        )
        assert status == 200, (
            "isolated mode must still archive the pinned profile's own session, got "
            f"{status} {payload!r}"
        )