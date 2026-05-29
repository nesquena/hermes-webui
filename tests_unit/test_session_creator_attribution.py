"""
Unit tests for session creator attribution (api/session_attribution.py).

Tests:
1. WebUI sessions correctly populate from the auth cookie.
2. Slack-forwarded sessions correctly populate from creator headers.
3. Sessions with no creator info default to {"source": "unknown"}.
4. /api/sessions returns the created_by object on every row (via compact()).
5. Kanban/cron/api creator builders.
6. Record + retrieve round-trip via sidecar DB.
7. Batch fetch (get_many_session_creators).
8. HERMES_CREATED_BY env-var path.

Run: python3 -m pytest tests/test_session_creator_attribution.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── Isolate all disk paths under a tmp dir ──────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="hermes_attr_test_")
os.environ.setdefault("HERMES_HOME", _TMP)
os.environ.setdefault("HERMES_WEBUI_STATE_DIR", _TMP)

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_attribution_db(tmp_path, monkeypatch):
    """Give every test a fresh attribution DB in a temp dir."""
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Force re-resolution of the path inside the module
    import importlib
    import api.session_attribution as sa
    importlib.reload(sa)
    yield sa


# ── Builder tests ─────────────────────────────────────────────────────────────

class TestBuilders:
    def test_webui_builder_with_user(self, isolated_attribution_db):
        sa = isolated_attribution_db
        user = {"id": "42", "email": "tony@workduo.com", "display_name": "Tony Wong"}
        cb = sa.build_webui_created_by(user, agent_identity="orchestrator")
        assert cb["source"] == "webui"
        assert cb["user_id"] == "42"
        assert cb["user_email"] == "tony@workduo.com"
        assert cb["display_name"] == "Tony Wong"
        assert cb["agent_identity"] == "orchestrator"
        assert cb["platform_user_id"] is None
        assert cb["created_at_iso"]  # non-empty

    def test_webui_builder_email_fallback(self, isolated_attribution_db):
        sa = isolated_attribution_db
        user = {"id": "1", "email": "alice@example.com"}  # no display_name
        cb = sa.build_webui_created_by(user)
        assert cb["display_name"] == "alice"  # email local-part

    def test_webui_builder_no_user(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_webui_created_by(None)
        assert cb["source"] == "unknown"
        assert cb.get("display_name") is None

    def test_slack_builder(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_slack_created_by(
            slack_user_id="U01ABC",
            display_name="Tony Wong",
            agent_identity="pm",
        )
        assert cb["source"] == "slack"
        assert cb["platform_user_id"] == "U01ABC"
        assert cb["display_name"] == "Tony Wong"
        assert cb["agent_identity"] == "pm"
        assert cb["user_id"] is None

    def test_kanban_builder(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_kanban_created_by("software-engineer")
        assert cb["source"] == "kanban"
        assert cb["agent_identity"] == "software-engineer"
        assert cb["user_id"] is None

    def test_cron_builder(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_cron_created_by("daily-digest")
        assert cb["source"] == "cron"
        assert cb["agent_identity"] == "daily-digest"

    def test_cron_builder_unnamed(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_cron_created_by(None)
        assert cb["source"] == "cron"
        assert cb["agent_identity"] == "cron-job"  # fallback

    def test_api_builder(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_api_created_by("my-integration")
        assert cb["source"] == "api"
        assert "my-integration" in cb["display_name"]


# ── Sidecar DB round-trip ─────────────────────────────────────────────────────

class TestSidecarDB:
    def test_record_and_retrieve(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_webui_created_by({"id": "5", "email": "bob@example.com"})
        sa.record_session_creator("sess_001", cb)
        result = sa.get_session_creator("sess_001")
        assert result["source"] == "webui"
        assert result["user_email"] == "bob@example.com"

    def test_missing_session_returns_unknown(self, isolated_attribution_db):
        sa = isolated_attribution_db
        result = sa.get_session_creator("nonexistent_session")
        assert result["source"] == "unknown"
        assert result.get("display_name") is None

    def test_empty_session_id_returns_unknown(self, isolated_attribution_db):
        sa = isolated_attribution_db
        result = sa.get_session_creator("")
        assert result["source"] == "unknown"

    def test_idempotent_record(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.build_slack_created_by("U01", "Alice")
        sa.record_session_creator("sess_002", cb)
        # Overwrite with updated display_name
        cb2 = sa.build_slack_created_by("U01", "Alice (Updated)")
        sa.record_session_creator("sess_002", cb2)
        result = sa.get_session_creator("sess_002")
        assert result["display_name"] == "Alice (Updated)"

    def test_batch_fetch(self, isolated_attribution_db):
        sa = isolated_attribution_db
        sa.record_session_creator("sess_10", sa.build_webui_created_by({"id": "1", "email": "a@b.com"}))
        sa.record_session_creator("sess_11", sa.build_kanban_created_by("pm"))
        result = sa.get_many_session_creators(["sess_10", "sess_11", "sess_missing"])
        assert result["sess_10"]["source"] == "webui"
        assert result["sess_11"]["source"] == "kanban"
        assert result["sess_missing"]["source"] == "unknown"

    def test_batch_fetch_empty(self, isolated_attribution_db):
        sa = isolated_attribution_db
        result = sa.get_many_session_creators([])
        assert result == {}

    def test_malformed_json_returns_unknown(self, isolated_attribution_db, tmp_path):
        sa = isolated_attribution_db
        # Manually insert malformed JSON to simulate corruption
        import sqlite3
        db_path = Path(os.environ["HERMES_HOME"]) / "webui" / "session_attribution.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_creators "
            "(session_id TEXT PRIMARY KEY, created_by_json TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO session_creators VALUES (?, ?, ?)",
            ("sess_bad", "not-valid-json", int(time.time())),
        )
        conn.commit()
        conn.close()
        result = sa.get_session_creator("sess_bad")
        assert result["source"] == "unknown"


# ── Header inference ──────────────────────────────────────────────────────────

class TestHeaderInference:
    def test_slack_headers(self, isolated_attribution_db):
        sa = isolated_attribution_db
        headers = {
            "X-Hermes-Creator-Source": "slack",
            "X-Hermes-Creator-User-Id": "U01ABC",
            "X-Hermes-Creator-Display-Name": "Tony Wong",
            "X-Hermes-Creator-Agent-Identity": "pm",
        }
        cb = sa.infer_creator_from_headers(headers)
        assert cb is not None
        assert cb["source"] == "slack"
        assert cb["platform_user_id"] == "U01ABC"
        assert cb["display_name"] == "Tony Wong"
        assert cb["agent_identity"] == "pm"

    def test_kanban_headers(self, isolated_attribution_db):
        sa = isolated_attribution_db
        headers = {
            "X-Hermes-Creator-Source": "kanban",
            "X-Hermes-Creator-Agent-Identity": "software-engineer",
        }
        cb = sa.infer_creator_from_headers(headers)
        assert cb is not None
        assert cb["source"] == "kanban"
        assert cb["agent_identity"] == "software-engineer"

    def test_no_headers_returns_none(self, isolated_attribution_db):
        sa = isolated_attribution_db
        cb = sa.infer_creator_from_headers({"Content-Type": "application/json"})
        assert cb is None

    def test_case_insensitive_headers(self, isolated_attribution_db):
        sa = isolated_attribution_db
        headers = {
            "x-hermes-creator-source": "cron",
            "x-hermes-creator-agent-identity": "daily-digest",
        }
        cb = sa.infer_creator_from_headers(headers)
        assert cb is not None
        assert cb["source"] == "cron"


# ── Env var inference ─────────────────────────────────────────────────────────

class TestEnvVarInference:
    def test_kanban_env_var(self, isolated_attribution_db, monkeypatch):
        sa = isolated_attribution_db
        payload = json.dumps({"source": "kanban", "agent_identity": "software-engineer"})
        monkeypatch.setenv("HERMES_CREATED_BY", payload)
        import importlib
        importlib.reload(sa)
        cb = sa.infer_creator_from_env()
        assert cb is not None
        assert cb["source"] == "kanban"
        assert cb["agent_identity"] == "software-engineer"
        assert cb["created_at_iso"]  # filled in by helper

    def test_missing_env_var_returns_none(self, isolated_attribution_db, monkeypatch):
        sa = isolated_attribution_db
        monkeypatch.delenv("HERMES_CREATED_BY", raising=False)
        cb = sa.infer_creator_from_env()
        assert cb is None

    def test_malformed_env_var_returns_none(self, isolated_attribution_db, monkeypatch):
        sa = isolated_attribution_db
        monkeypatch.setenv("HERMES_CREATED_BY", "not-json")
        cb = sa.infer_creator_from_env()
        assert cb is None


# ── Session model integration ─────────────────────────────────────────────────

class TestSessionModelIntegration:
    def test_session_stores_created_by(self, isolated_attribution_db):
        """Session.compact() includes created_by."""
        import api.models as models
        cb = {"source": "webui", "user_email": "t@test.com", "created_at_iso": "2026-01-01T00:00:00Z"}
        s = models.Session(
            session_id="test_cb_session",
            created_by=cb,
        )
        compact = s.compact()
        assert compact["created_by"] == cb

    def test_session_none_created_by(self, isolated_attribution_db):
        """Sessions without created_by have compact()['created_by'] == None."""
        import api.models as models
        s = models.Session(session_id="legacy_session")
        compact = s.compact()
        # None is stored and serialized; /api/sessions handler coerces it to unknown
        assert compact["created_by"] is None

    def test_session_roundtrip(self, isolated_attribution_db, tmp_path, monkeypatch):
        """Session saves and loads created_by correctly."""
        import api.models as models
        # Override SESSION_DIR and SESSION_INDEX_FILE to avoid touching real disk
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        index_file = session_dir / "_index.json"
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
        cb = {"source": "kanban", "agent_identity": "software-engineer", "created_at_iso": "2026-01-01T00:00:00Z"}
        s = models.Session(session_id="roundtrip_test", created_by=cb)
        s.messages = [{"role": "user", "content": "hi", "timestamp": time.time()}]
        s.save()
        loaded = models.Session.load("roundtrip_test")
        assert loaded is not None
        assert isinstance(loaded.created_by, dict)
        assert loaded.created_by["source"] == "kanban"
