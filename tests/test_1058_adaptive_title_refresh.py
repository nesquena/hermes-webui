"""Tests for adaptive session title refresh helpers (PR #1058).

Covers all five new functions added to api/streaming.py:
  - _count_exchanges
  - _latest_exchange_snippets
  - _get_title_refresh_interval
  - _run_background_title_refresh
  - _maybe_schedule_title_refresh
"""

import sys
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.streaming import (
    _count_exchanges,
    _latest_exchange_snippets,
    _get_title_refresh_interval,
    _run_background_title_refresh,
    _maybe_schedule_title_refresh,
)


@pytest.fixture(autouse=True)
def _restore_auth_sessions():
    """Snapshot and restore api.auth._sessions around each test.

    Importing api.streaming can trigger api.config.load_settings() which may
    call into api.auth and create a real session token.  Without this fixture,
    that stale token leaks into test_auth_session_persistence.py tests (which
    assume _sessions starts empty) when our file runs first alphabetically.
    """
    import api.auth as _auth

    snapshot = dict(_auth._sessions)
    yield
    _auth._sessions.clear()
    _auth._sessions.update(snapshot)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_msg(text):
    return {"role": "user", "content": text}


def _asst_msg(text, tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_only_asst():
    """Assistant message that only has tool_calls, no real text."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "type": "function"}],
    }


def _make_session(
    title="My Title", llm_title_generated=True, messages=None, session_id="sid1"
):
    s = MagicMock()
    s.title = title
    s.llm_title_generated = llm_title_generated
    s.messages = messages or []
    s.session_id = session_id
    s.save = MagicMock()
    return s


# ---------------------------------------------------------------------------
# _count_exchanges
# ---------------------------------------------------------------------------


class TestCountExchanges:
    def test_empty_messages_returns_zero(self):
        assert _count_exchanges([]) == 0

    def test_none_messages_returns_zero(self):
        assert _count_exchanges(None) == 0

    def test_counts_only_user_messages(self):
        msgs = [_user_msg("hello"), _asst_msg("hi"), _user_msg("world")]
        assert _count_exchanges(msgs) == 2

    def test_skips_empty_user_messages(self):
        msgs = [_user_msg(""), _user_msg("   "), _user_msg("real question")]
        assert _count_exchanges(msgs) == 1

    def test_counts_list_content_user_messages(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "list question"}]},
        ]
        assert _count_exchanges(msgs) == 1

    def test_skips_empty_list_content(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "   "}]},
        ]
        assert _count_exchanges(msgs) == 0

    def test_ignores_non_user_roles(self):
        msgs = [_asst_msg("response"), {"role": "system", "content": "system prompt"}]
        assert _count_exchanges(msgs) == 0

    def test_ignores_non_dict_entries(self):
        msgs = ["not a dict", _user_msg("real"), None]
        assert _count_exchanges(msgs) == 1

    def test_five_exchanges(self):
        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"question {i}"))
            msgs.append(_asst_msg(f"answer {i}"))
        assert _count_exchanges(msgs) == 5


# ---------------------------------------------------------------------------
# _latest_exchange_snippets
# ---------------------------------------------------------------------------


class TestLatestExchangeSnippets:
    def test_empty_returns_empty_strings(self):
        u, a = _latest_exchange_snippets([])
        assert u == "" and a == ""

    def test_none_returns_empty_strings(self):
        u, a = _latest_exchange_snippets(None)
        assert u == "" and a == ""

    def test_basic_pair(self):
        msgs = [
            _user_msg("first q"),
            _asst_msg("first a"),
            _user_msg("second q"),
            _asst_msg("second a"),
        ]
        u, a = _latest_exchange_snippets(msgs)
        assert u == "second q"
        assert a == "second a"

    def test_returns_latest_not_first(self):
        msgs = [
            _user_msg("old q"),
            _asst_msg("old a"),
            _user_msg("new q"),
            _asst_msg("new a"),
        ]
        u, a = _latest_exchange_snippets(msgs)
        assert u == "new q"

    def test_skips_tool_call_only_assistant(self):
        """An assistant msg with tool_calls and no real text should be skipped."""
        msgs = [
            _user_msg("q"),
            _asst_msg("real answer"),
            _user_msg("q2"),
            _tool_only_asst(),
        ]
        u, a = _latest_exchange_snippets(msgs)
        # _tool_only_asst should be skipped; fall back to previous real assistant
        assert a == "real answer"
        assert u == "q2"

    def test_truncates_long_content(self):
        long_text = "x" * 600
        msgs = [_user_msg(long_text), _asst_msg(long_text)]
        u, a = _latest_exchange_snippets(msgs)
        assert len(u) == 500
        assert len(a) == 500

    def test_no_assistant_message(self):
        msgs = [_user_msg("q")]
        u, a = _latest_exchange_snippets(msgs)
        assert u == "q"
        assert a == ""

    def test_no_user_message(self):
        msgs = [_asst_msg("a")]
        u, a = _latest_exchange_snippets(msgs)
        assert u == ""
        assert a == "a"

    def test_ignores_non_dict_entries(self):
        msgs = ["noise", _user_msg("q"), None, _asst_msg("a")]
        u, a = _latest_exchange_snippets(msgs)
        assert u == "q"
        assert a == "a"


# ---------------------------------------------------------------------------
# _get_title_refresh_interval
# ---------------------------------------------------------------------------


class TestGetTitleRefreshInterval:
    def test_returns_int_for_valid_setting(self):
        # _get_title_refresh_interval does a local import: `from api.config import load_settings`
        # so patch the source module, not api.streaming
        with patch(
            "api.config.load_settings", return_value={"auto_title_refresh_every": "5"}
        ):
            assert _get_title_refresh_interval() == 5

    def test_returns_zero_for_off_setting(self):
        with patch(
            "api.config.load_settings", return_value={"auto_title_refresh_every": "0"}
        ):
            assert _get_title_refresh_interval() == 0

    def test_returns_zero_when_key_absent(self):
        with patch("api.config.load_settings", return_value={}):
            assert _get_title_refresh_interval() == 0

    def test_returns_zero_on_exception(self):
        with patch("api.config.load_settings", side_effect=Exception("boom")):
            assert _get_title_refresh_interval() == 0

    def test_valid_values_10_and_20(self):
        for val in ("10", "20"):
            with patch(
                "api.config.load_settings",
                return_value={"auto_title_refresh_every": val},
            ):
                assert _get_title_refresh_interval() == int(val)


# ---------------------------------------------------------------------------
# _run_background_title_refresh
# ---------------------------------------------------------------------------


class TestRunBackgroundTitleRefresh:
    def _make_put_event(self):
        events = []

        def put(name, data):
            events.append((name, data))

        return put, events

    def _make_session_obj(self, title="Old Title"):
        s = MagicMock()
        s.title = title
        s.llm_title_generated = True
        s.save = MagicMock()
        return s

    def test_skips_when_title_changed_before_call(self):
        """If the title has changed (manual rename) since the refresh was scheduled, skip."""
        put, events = self._make_put_event()
        with (
            patch("api.streaming.get_session") as mock_get,
            patch("api.streaming.SESSIONS", {}),
            patch("api.streaming.LOCK", threading.Lock()),
        ):
            s = self._make_session_obj(title="Different Title")
            mock_get.return_value = s
            _run_background_title_refresh(
                "sid", "user", "asst", "Old Title", put, agent=None
            )
        # No 'title' event should have been emitted
        assert not any(name == "title" for name, _ in events)

    def test_skips_if_session_not_found(self):
        put, events = self._make_put_event()
        with patch("api.streaming.get_session", side_effect=KeyError("not found")):
            _run_background_title_refresh("sid", "u", "a", "title", put)
        assert events == []

    def test_skips_when_title_is_untitled(self):
        put, events = self._make_put_event()
        with patch("api.streaming.get_session") as mock_get:
            s = self._make_session_obj(title="Untitled")
            mock_get.return_value = s
            _run_background_title_refresh("sid", "u", "a", "Untitled", put)
        assert not any(name == "title" for name, _ in events)

    def test_skips_same_title(self):
        """If the LLM generates a title identical to the current one, no event is emitted."""
        put, events = self._make_put_event()
        with (
            patch("api.streaming.get_session") as mock_get,
            patch("api.streaming._aux_title_configured", return_value=True),
            patch(
                "api.streaming._generate_llm_session_title_via_aux",
                return_value=("Old Title", "llm_ok", "raw"),
            ),
            patch("api.streaming.SESSIONS", {}),
            patch("api.streaming.LOCK", threading.Lock()),
        ):
            s = self._make_session_obj(title="Old Title")
            mock_get.return_value = s
            _run_background_title_refresh("sid", "u", "a", "Old Title", put)
        assert not any(name == "title" for name, _ in events)

    def test_emits_title_event_on_new_title(self):
        put, events = self._make_put_event()
        s = self._make_session_obj(title="Old Title")
        # Use a real dict for SESSIONS so .get() works, pre-populated with our session
        fake_sessions = {"sid": s}
        with (
            patch("api.streaming.get_session", return_value=s),
            patch("api.streaming._aux_title_configured", return_value=True),
            patch(
                "api.streaming._generate_llm_session_title_via_aux",
                return_value=("New Refreshed Title", "llm_ok", "raw"),
            ),
            patch("api.streaming.SESSIONS", fake_sessions),
            patch("api.streaming.LOCK", threading.Lock()),
        ):
            _run_background_title_refresh("sid", "u", "a", "Old Title", put)
        title_events = [(n, d) for n, d in events if n == "title"]
        assert len(title_events) == 1
        assert title_events[0][1]["title"] == "New Refreshed Title"

    def test_saves_refreshed_title_outside_global_lock(self):
        """Refreshing an existing title must not call Session.save() while holding LOCK."""

        class TrackingLock:
            def __init__(self):
                self.held = False

            def __enter__(self):
                assert not self.held
                self.held = True
                return self

            def __exit__(self, exc_type, exc, tb):
                self.held = False

        put, events = self._make_put_event()
        lock = TrackingLock()
        s = self._make_session_obj(title="Old Title")

        def save(*args, **kwargs):
            assert not lock.held, "Session.save() must run outside api.models.LOCK"

        s.save = save
        fake_sessions = {"sid": s}
        with (
            patch("api.streaming.get_session", return_value=s),
            patch("api.streaming._aux_title_configured", return_value=True),
            patch(
                "api.streaming._generate_llm_session_title_via_aux",
                return_value=("New Refreshed Title", "llm_ok", "raw"),
            ),
            patch("api.streaming.SESSIONS", fake_sessions),
            patch("api.streaming.LOCK", lock),
        ):
            _run_background_title_refresh("sid", "u", "a", "Old Title", put)
        title_events = [(n, d) for n, d in events if n == "title"]
        assert len(title_events) == 1
        assert title_events[0][1]["title"] == "New Refreshed Title"

    def test_exceptions_are_silently_swallowed(self):
        """Any unexpected error inside must not propagate — it's a background daemon."""
        put, events = self._make_put_event()
        with patch("api.streaming.get_session", side_effect=RuntimeError("oops")):
            # Should not raise
            _run_background_title_refresh("sid", "u", "a", "title", put)
        assert events == []


# ---------------------------------------------------------------------------
# _maybe_schedule_title_refresh
# ---------------------------------------------------------------------------


class TestMaybeScheduleTitleRefresh:
    def _noop_put(self, name, data):
        pass

    def test_does_nothing_when_disabled(self):
        with patch("api.streaming._get_title_refresh_interval", return_value=0):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                session = _make_session(messages=[_user_msg("q"), _asst_msg("a")] * 5)
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []

    def test_does_nothing_when_title_is_empty(self):
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                session = _make_session(
                    title="", messages=[_user_msg("q"), _asst_msg("a")] * 5
                )
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []

    def test_does_nothing_for_untitled(self):
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                session = _make_session(
                    title="Untitled", messages=[_user_msg("q"), _asst_msg("a")] * 5
                )
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []

    def test_does_nothing_when_title_not_llm_generated(self):
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                session = _make_session(
                    llm_title_generated=False,
                    messages=[_user_msg("q"), _asst_msg("a")] * 5,
                )
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []

    def test_does_nothing_when_exchange_count_not_at_interval(self):
        """Refresh only fires when exchange_count % interval == 0 (and > 0)."""
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                # 4 exchanges — not a multiple of 5
                session = _make_session(messages=[_user_msg("q"), _asst_msg("a")] * 4)
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []

    def test_spawns_thread_at_exact_interval(self):
        """Refresh fires when exchange_count == refresh_interval."""
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            spawned = []
            with patch("threading.Thread") as mock_thread_cls:
                mock_thread = MagicMock()
                mock_thread_cls.return_value = mock_thread
                # 5 user messages = 5 exchanges
                session = _make_session(messages=[_user_msg("q"), _asst_msg("a")] * 5)
                _maybe_schedule_title_refresh(session, self._noop_put, None)
                assert mock_thread_cls.called
                assert mock_thread.start.called

    def test_spawns_thread_at_multiple_of_interval(self):
        """Refresh fires at 10 exchanges when interval is 5."""
        with patch("api.streaming._get_title_refresh_interval", return_value=5):
            with patch("threading.Thread") as mock_thread_cls:
                mock_thread = MagicMock()
                mock_thread_cls.return_value = mock_thread
                # 10 exchanges
                session = _make_session(messages=[_user_msg("q"), _asst_msg("a")] * 10)
                _maybe_schedule_title_refresh(session, self._noop_put, None)
                assert mock_thread_cls.called

    def test_does_nothing_when_no_exchange_content(self):
        """Even at interval, if both snippets are empty, don't spawn."""
        with (
            patch("api.streaming._get_title_refresh_interval", return_value=5),
            patch("api.streaming._latest_exchange_snippets", return_value=("", "")),
        ):
            spawned = []
            with patch(
                "threading.Thread",
                side_effect=lambda **kw: spawned.append(kw) or MagicMock(),
            ):
                session = _make_session(messages=[_user_msg("q"), _asst_msg("a")] * 5)
                _maybe_schedule_title_refresh(session, self._noop_put, None)
        assert spawned == []
