"""Metadata observation must not construct full-transcript regeneration authority."""

import copy
import sqlite3
import tempfile
import unittest
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


class SessionMetadataRegenerationTests(unittest.TestCase):
    def setUp(self):
        from api import config, models, profiles, routes

        self.models = models
        self.routes = routes
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.session_dir = self.home / "sessions"
        self.session_dir.mkdir()
        for module in (config, models):
            self.enterContext(patch.object(module, "SESSION_DIR", self.session_dir))
            self.enterContext(patch.object(module, "SESSION_INDEX_FILE", self.session_dir / "_index.json"))
        self.enterContext(patch.object(models, "SESSIONS", OrderedDict()))
        self.enterContext(patch.object(profiles, "get_active_hermes_home", return_value=self.home))
        self.enterContext(patch.object(profiles, "get_hermes_home_for_profile", return_value=self.home))
        self.enterContext(patch.object(models, "_active_state_db_path", return_value=self.home / "state.db"))
        self.enterContext(patch.object(routes, "_get_active_profile_name", return_value="metadata-test"))
        self.enterContext(patch.object(routes, "_clear_stale_stream_state", return_value=False))
        self.enterContext(patch.object(routes, "_lookup_cli_session_metadata", return_value={}))
        self.rows = [
            {"role": "user", "content": "prompt", "timestamp": 1000.0, "_source": "webui"},
            {"role": "assistant", "content": "answer", "timestamp": 1001.0},
        ]
        self.session = models.Session(
            session_id="metadata-regeneration",
            profile="metadata-test",
            messages=copy.deepcopy(self.rows),
            context_messages=copy.deepcopy(self.rows),
            context_length=4096,
            created_at=1000.0,
            updated_at=1001.0,
        )
        self.session.save(touch_updated_at=False)
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)")
            db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)")
            db.execute("INSERT INTO sessions VALUES (?, 'webui', 'Metadata', 'test-model', 1000, 2)", (self.session.session_id,))
        self.append_rows(self.rows)
        self.get_session = self.enterContext(patch.object(routes, "get_session", return_value=self.session))
        self.history = self.enterContext(patch.object(
            models, "get_state_db_session_messages", wraps=models.get_state_db_session_messages,
        ))

    def append_rows(self, rows):
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.executemany(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                [(self.session.session_id, row["role"], row["content"], row["timestamp"]) for row in rows],
            )
            db.execute("UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages)")

    def request(self, query):
        with patch.object(self.routes, "j") as response:
            self.routes._handle_session_get(
                SimpleNamespace(_safe_webui_print=lambda *_args: None),
                urlparse(f"/api/session?session_id={self.session.session_id}&resolve_model=0&{query}"),
            )
        self.assertEqual(response.call_args.kwargs.get("status", 200), 200)
        return response.call_args.args[1]["session"]

    def test_metadata_cold_load_omits_revision_without_canonical_history(self):
        self.get_session.return_value = self.models.Session.load_metadata_only(self.session.session_id)
        payload = self.request("messages=0")
        self.get_session.assert_called_once_with(self.session.session_id, metadata_only=True)
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["message_count"], 2)
        self.assertNotIn("regeneration_revision", payload)
        self.history.assert_not_called()

    def test_metadata_warm_load_omits_revision_without_canonical_history(self):
        payload = self.request("messages=0")
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["message_count"], 2)
        self.assertNotIn("regeneration_revision", payload)
        self.history.assert_not_called()

    def test_metadata_summary_still_observes_external_append(self):
        initial = self.request("messages=0")
        self.append_rows([
            {"role": "user", "content": "external prompt", "timestamp": 1002.0},
            {"role": "assistant", "content": "external answer", "timestamp": 1003.0},
        ])
        refreshed = self.request("messages=0")
        self.assertEqual(initial["message_count"], 2)
        self.assertEqual(refreshed["message_count"], 4)
        self.assertEqual(refreshed["last_message_at"], 1003.0)
        self.assertEqual(refreshed["messages"], [])
        self.history.assert_not_called()

    def test_full_load_retains_canonical_revision(self):
        from api.session_ops import resolve_regeneration_turn

        for query in ("messages=1", "messages=1&msg_limit=all", "messages=1&msg_limit=30"):
            with self.subTest(query=query):
                payload = self.request(query)
                self.assertEqual(len(payload["messages"]), 2)
                self.assertFalse(payload["_messages_truncated"])
                revision = payload["regeneration_revision"]
                self.assertTrue(revision)
                turn = resolve_regeneration_turn(self.session, expected_revision=revision)
                self.assertEqual(turn.message["content"], "prompt")
        self.assertTrue(self.history.called)

    def test_truncated_and_read_only_loads_omit_authority(self):
        for query in ("messages=1&msg_limit=1", "messages=1&msg_limit=1&msg_before=2"):
            with self.subTest(query=query):
                payload = self.request(query)
                self.assertTrue(payload["_messages_truncated"])
                self.assertNotIn("regeneration_revision", payload)
        self.history.assert_not_called()
        self.session.read_only = True
        payload = self.request("messages=1&msg_limit=all")
        self.assertNotIn("regeneration_revision", payload)
        self.history.assert_not_called()

    def test_imported_owned_turn_requires_full_load(self):
        from api.process_event_utils import build_active_turn_token

        self.session.is_cli_session = True
        self.session.session_source = "desktop"
        self.session.messages[0]["_source"] = "desktop"
        payload = self.request("messages=1&msg_limit=all")
        self.assertNotIn("regeneration_revision", payload)
        self.session.messages[0]["_active_turn_token"] = build_active_turn_token("imported-stream", 123.0)
        self.assertNotIn("regeneration_revision", self.request("messages=0"))
        self.history.assert_not_called()
        self.assertTrue(self.request("messages=1&msg_limit=all")["regeneration_revision"])

    def test_summary_uses_session_profile_even_with_a_different_active_home(self):
        from api import profiles

        other_home = self.home / "other-profile"
        other_home.mkdir()
        with closing(sqlite3.connect(other_home / "state.db")) as db, db:
            db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
            db.execute("INSERT INTO messages VALUES (?, 'user', 'other profile', 2000)", (self.session.session_id,))
        self.append_rows([
            {"role": "assistant", "content": "own profile addition", "timestamp": 1002.0},
        ])
        with patch.object(profiles, "get_active_hermes_home", return_value=other_home), patch.object(
            profiles, "get_hermes_home_for_profile",
            side_effect=lambda name: self.home if name == "metadata-test" else other_home,
        ), patch.object(self.models, "_active_state_db_path", return_value=other_home / "state.db"):
            payload = self.request("messages=0")
        self.assertEqual(payload["message_count"], 3)
        self.assertEqual(payload["last_message_at"], 1002.0)
        self.history.assert_not_called()

    def test_profile_mismatch_exits_before_summary_and_authority(self):
        self.session.profile = "other-profile"
        with patch.object(self.routes, "_metadata_only_message_summary") as summary, patch.object(self.routes, "j") as response:
            self.routes._handle_session_get(
                SimpleNamespace(), urlparse(f"/api/session?session_id={self.session.session_id}&messages=0"),
            )
        self.assertEqual(response.call_args.kwargs["status"], 409)
        summary.assert_not_called()
        self.history.assert_not_called()


if __name__ == "__main__":
    unittest.main()
