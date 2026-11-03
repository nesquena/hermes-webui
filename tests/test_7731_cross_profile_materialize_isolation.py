"""#7731: cross-profile isolation for _get_or_materialize_session and archive.

The reviewer gate requires:
  1. An unqualified request from profile A must NOT discover/materialize
     profile B's CLI session through _get_or_materialize_session().
  2. Archive must only do a cross-profile lookup when the request carries
     ``all_profiles=true`` AND a validated ``profile`` equal to the owning
     profile; otherwise return 404 or 409 session_profile_mismatch.
  3. A behavioral test exercising both halves.
"""

import collections
import io
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def models_module():
    return pytest.importorskip("api.models")


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    """Wire up state.db + SESSION_INDEX_FILE + SESSION_DIR to tmp_path."""
    db = tmp_path / "state.db"
    state_dir = tmp_path / "webui-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")
    import api.routes as _routes
    import api.models as _models
    monkeypatch.setattr(_models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(_routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    return {
        "db": db,
        "state_dir": state_dir,
        "sessions_dir": sessions_dir,
        "index_path": index_path,
    }


def _make_state_db(path, sid, *, message_count=2, title="tui session",
                   model="MiniMax-M3", source="tui", cwd="/root", profile=None):
    """Create a minimal state.db with one session and messages."""
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
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
    """)
    conn.execute(
        "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
        "VALUES (?, ?, ?, ?, 1781024055.0, ?, ?)",
        (sid, source, model, message_count, title, cwd),
    )
    for i in range(message_count):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant",
             f"msg {i}", 1781024055.0 + i),
        )
    conn.commit()
    conn.close()


class _FakePostHandler:
    def __init__(self, body, *, path):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.response_headers = {}
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _response_json(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMaterializeActiveProfileOnly:
    """_get_or_materialize_session() must be active-profile-only."""

    def test_unqualified_materialize_returns_404_for_foreign_session(
        self, routes_module, isolated_state_db, monkeypatch
    ):
        """Profile A requests rename of profile B's session → 404, no sidecar created."""
        _make_state_db(
            isolated_state_db["db"], "cli_bravo_only_0001",
            title="Bravo private", source="tui", message_count=2,
        )
        # Mock _resolve_cli_import_metadata to return the foreign session
        # only when allow_all_profiles=True (the old behaviour).
        # With the fix, _get_or_materialize_session calls it without that flag.
        foreign_meta = {
            "session_id": "cli_bravo_only_0001",
            "title": "Bravo private",
            "model": "MiniMax-M3",
            "source": "tui",
            "profile": "bravo",
            "message_count": 2,
        }

        def _mock_resolve(sid, *, requested_profile=None, allow_all_profiles=False):
            if allow_all_profiles:
                return foreign_meta
            return {}

        monkeypatch.setattr(routes_module, "_resolve_cli_import_metadata", _mock_resolve)
        monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                            lambda sid, **_kw: {})
        monkeypatch.setattr(routes_module, "get_session",
                            lambda *a, **kw: (_ for _ in ()).throw(KeyError))

        with pytest.raises(KeyError):
            routes_module._get_or_materialize_session("cli_bravo_only_0001")

    def test_active_profile_materialize_succeeds(self, routes_module, isolated_state_db, monkeypatch):
        """Profile A requests rename of profile A's own session → succeeds."""
        _make_state_db(
            isolated_state_db["db"], "cli_alpha_0001",
            title="Alpha session", source="tui", message_count=2,
        )
        own_meta = {
            "session_id": "cli_alpha_0001",
            "title": "Alpha session",
            "model": "MiniMax-M3",
            "source": "tui",
            "profile": "alpha",
            "message_count": 2,
        }
        monkeypatch.setattr(routes_module, "_resolve_cli_import_metadata",
                            lambda sid, **_kw: own_meta)
        monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                            lambda sid, **_kw: {})
        monkeypatch.setattr(routes_module, "get_cli_session_messages",
                            lambda sid, profile=None: [{"role": "user", "content": "hi", "timestamp": 1.0}])
        monkeypatch.setattr(routes_module, "get_last_workspace", lambda **_kw: "/tmp")
        monkeypatch.setattr(routes_module, "import_cli_session",
                            lambda sid, title, msgs, model, **kw: type("S", (), {
                                "session_id": sid, "title": title, "model": model,
                                "is_cli_session": True, "source_tag": None,
                                "raw_source": None, "session_source": None,
                                "source_label": None, "user_id": None,
                                "chat_id": None, "chat_type": None,
                                "thread_id": None, "session_key": None,
                                "platform": None, "archived": False,
                                "profile": kw.get("profile"),
                                "save": lambda self, **_kw: None,
                            })())

        s = routes_module._get_or_materialize_session("cli_alpha_0001")
        assert s.session_id == "cli_alpha_0001"

    def test_materialize_function_is_active_profile_only(self):
        """Source code check: _get_or_materialize_session must NOT pass allow_all_profiles=True."""
        import re
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        # Find the fallback section in _get_or_materialize_session
        start = src.index("def _get_or_materialize_session(")
        # Find the next top-level def to bound the search
        next_def = re.search(r"\ndef [a-z_]", src[start + 40:])
        block = src[start:start + 40 + next_def.start() if next_def else 5000]
        # The only _resolve_cli_import_metadata call in the materializer
        # must NOT carry allow_all_profiles=True
        assert "allow_all_profiles=True" not in block, (
            "_get_or_materialize_session still passes allow_all_profiles=True "
            "to _resolve_cli_import_metadata — profile isolation is broken (#7731)"
        )


class TestArchiveRequestScoped:
    """Archive cross-profile lookup must be gated on request body."""

    def test_unqualified_archive_returns_404_for_foreign_session(
        self, routes_module, isolated_state_db, monkeypatch
    ):
        """POST /api/session/archive without all_profiles → 404 for foreign session."""
        foreign_meta = {
            "session_id": "cli_bravo_only_0002",
            "title": "Bravo private",
            "model": "MiniMax-M3",
            "source": "tui",
            "profile": "bravo",
            "message_count": 2,
        }

        def _mock_resolve(sid, *, requested_profile=None, allow_all_profiles=False):
            if allow_all_profiles:
                return foreign_meta
            return {}

        monkeypatch.setattr(routes_module, "_resolve_cli_import_metadata", _mock_resolve)
        monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                            lambda sid, **_kw: {})
        monkeypatch.setattr(routes_module, "get_session", lambda *a, **kw: (_ for _ in ()).throw(KeyError))
        monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda sid: False)

        handler = _FakePostHandler(
            {"session_id": "cli_bravo_only_0002", "archived": True},
            path="/api/session/archive",
        )
        from urllib.parse import urlparse
        routes_module.handle_post(handler, urlparse("/api/session/archive"))
        assert handler.status == 404

    def test_scoped_archive_succeeds_for_foreign_session(
        self, routes_module, isolated_state_db, monkeypatch
    ):
        """POST /api/session/archive with all_profiles+profile → 200 for foreign session."""
        foreign_meta = {
            "session_id": "cli_bravo_only_0003",
            "title": "Bravo private",
            "model": "MiniMax-M3",
            "source": "tui",
            "profile": "bravo",
            "message_count": 2,
            "created_at": 1781024055.0,
            "updated_at": 1781024055.0,
        }

        def _mock_resolve(sid, *, requested_profile=None, allow_all_profiles=False):
            if allow_all_profiles and sid == "cli_bravo_only_0003":
                return foreign_meta
            return {}

        monkeypatch.setattr(routes_module, "_resolve_cli_import_metadata", _mock_resolve)
        monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                            lambda sid, **_kw: {})
        monkeypatch.setattr(routes_module, "get_session", lambda *a, **kw: (_ for _ in ()).throw(KeyError))
        monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda sid: False)
        monkeypatch.setattr(routes_module, "get_cli_session_messages",
                            lambda sid, profile=None: [{"role": "user", "content": "hi", "timestamp": 1.0}])
        monkeypatch.setattr(routes_module, "get_last_workspace", lambda **_kw: "/tmp")
        def _fake_import(sid, title, msgs, model, **kw):
            # Master's #7738 archive flow re-resolves the session under the
            # lock after materializing, so the fake must be resident in
            # SESSIONS exactly as a real import_cli_session save would be.
            sess = type("S", (), {
                "session_id": sid, "title": title, "model": model,
                "is_cli_session": True, "source_tag": None,
                "raw_source": None, "session_source": None,
                "source_label": None, "user_id": None,
                "chat_id": None, "chat_type": None,
                "thread_id": None, "session_key": None,
                "platform": None, "archived": False,
                "profile": kw.get("profile"),
                "compact": lambda self: {"session_id": self.session_id},
                "save": lambda self, **_kw: None,
            })()
            routes_module.SESSIONS[sid] = sess
            return sess
        monkeypatch.setattr(routes_module, "import_cli_session", _fake_import)
        # The fake import publishes into the shared SESSIONS cache, which the
        # autouse cleanup fixture does NOT restore (it only deletes through the
        # separate test server). A later in-process all_sessions() would call
        # this zero-argument compact() with kwargs and raise TypeError. Give
        # this test a private cache that monkeypatch rolls back afterwards.
        monkeypatch.setattr(routes_module, "SESSIONS", collections.OrderedDict())
        _lock_obj = type("L", (), {"acquire": lambda s, timeout=0: True, "release": lambda s: None, "__enter__": lambda s: s, "__exit__": lambda s, *a: None})()
        monkeypatch.setattr(routes_module, "_get_session_agent_lock",
                            lambda sid: _lock_obj)
        monkeypatch.setattr(routes_module, "publish_session_list_changed",
                            lambda *a, **kw: None)
        monkeypatch.setattr(routes_module, "_worktree_retained_payload", lambda s: {})

        handler = _FakePostHandler(
            {"session_id": "cli_bravo_only_0003", "archived": True,
             "all_profiles": True, "profile": "bravo"},
            path="/api/session/archive",
        )
        from urllib.parse import urlparse
        routes_module.handle_post(handler, urlparse("/api/session/archive"))
        assert handler.status == 200

    def test_archive_profile_mismatch_returns_409(
        self, routes_module, isolated_state_db, monkeypatch
    ):
        """POST /api/session/archive with all_profiles but wrong profile → 409."""
        foreign_meta = {
            "session_id": "cli_bravo_only_0004",
            "title": "Bravo private",
            "model": "MiniMax-M3",
            "source": "tui",
            "profile": "bravo",
            "message_count": 2,
        }

        def _mock_resolve(sid, *, requested_profile=None, allow_all_profiles=False):
            if allow_all_profiles:
                return foreign_meta
            return {}

        monkeypatch.setattr(routes_module, "_resolve_cli_import_metadata", _mock_resolve)
        monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                            lambda sid, **_kw: {})
        monkeypatch.setattr(routes_module, "get_session", lambda *a, **kw: (_ for _ in ()).throw(KeyError))
        monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda sid: False)

        handler = _FakePostHandler(
            {"session_id": "cli_bravo_only_0004", "archived": True,
             "all_profiles": True, "profile": "alpha"},  # wrong profile!
            path="/api/session/archive",
        )
        from urllib.parse import urlparse
        routes_module.handle_post(handler, urlparse("/api/session/archive"))
        assert handler.status == 409
        resp = _response_json(handler)
        assert resp.get("code") == "session_profile_mismatch"

    def test_archive_source_code_gated(self):
        """Source code check: archive must read all_profiles from body, not hardcode allow_all_profiles=True."""
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        # Find the archive handler block
        archive_idx = src.index('/api/session/archive')
        # Search within ~500 chars after for the _resolve_cli_import_metadata call
        block = src[archive_idx:archive_idx + 1500]
        # The archive handler must NOT hardcode allow_all_profiles=True
        assert "allow_all_profiles=True" not in block, (
            "Archive handler still hardcodes allow_all_profiles=True — "
            "must read from request body (#7731)"
        )
