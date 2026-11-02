"""Coverage for claiming durable background events this WebUI host owns.

Events published by a WebUI-hosted turn land in the shared ``background_events``
ledger with ``platform='webui'``. Before ``recover_durable_webui_events`` the only
consumer this host drained was the in-process ``completion_queue``, so anything
published before a WebUI restart was never delivered here — and the core gateway
could not route ``platform='webui'`` either, so the rows sat pending until they
aged out at 48h, losing permission prompts and completion notices.
"""

import json
import time

import pytest

from api import background_process as bp

pytest.importorskip("tools.background_events")


def _ledger_conn():
    """Connect through core so the ledger schema is created if absent."""
    from tools.background_events import _connect

    return _connect()


def _insert_pending(ledger_id: str, evt: dict) -> None:
    now = time.time()
    conn = _ledger_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO background_events
               (ledger_id, plugin_id, event_id, event_json, created_at,
                updated_at, delivery_state, delivery_attempts)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)""",
            (
                ledger_id,
                "claude-sessions",
                ledger_id,
                json.dumps(evt),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _state(ledger_id: str) -> str:
    conn = _ledger_conn()
    try:
        row = conn.execute(
            "SELECT delivery_state FROM background_events WHERE ledger_id=?",
            (ledger_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def _clear_ledger() -> None:
    conn = _ledger_conn()
    try:
        conn.execute("DELETE FROM background_events")
        conn.commit()
    finally:
        conn.close()


def _webui_event(ledger_id: str, **overrides) -> dict:
    evt = {
        "type": "background_event",
        "delegation_id": ledger_id,
        "event_id": ledger_id,
        "kind": "permission_required",
        "message": "Managed Claude session is waiting for a permission decision.",
        "platform": "webui",
        "chat_id": "c62ef7c6baba",
        "chat_type": "",
        "session_key": "c62ef7c6baba",
        "origin_ui_session_id": "c62ef7c6baba",
        "parent_session_id": "c62ef7c6baba",
    }
    evt.update(overrides)
    return evt


@pytest.fixture(autouse=True)
def _clean_ledger():
    _clear_ledger()
    yield
    _clear_ledger()


def test_claims_and_delivers_a_pending_webui_event(monkeypatch):
    evt = _webui_event("plugin:claude-sessions:permission-1")
    _insert_pending("plugin:claude-sessions:permission-1", evt)

    delivered = []
    monkeypatch.setattr(bp, "_process_one", lambda e: delivered.append(e))

    assert bp.recover_durable_webui_events() == 1
    assert len(delivered) == 1
    assert delivered[0]["origin_ui_session_id"] == "c62ef7c6baba"
    assert _state("plugin:claude-sessions:permission-1") == "delivered"


def test_leaves_gateway_routed_events_for_the_gateway(monkeypatch):
    # A Telegram-routed event is deliverable by the core gateway; claiming it here
    # would steal it from the only consumer that can actually route it.
    evt = _webui_event(
        "plugin:claude-sessions:telegram-1",
        platform="telegram",
        chat_type="dm",
        chat_id="1234567890",
    )
    _insert_pending("plugin:claude-sessions:telegram-1", evt)

    monkeypatch.setattr(bp, "_process_one", lambda e: pytest.fail("must not deliver"))

    assert bp.recover_durable_webui_events() == 0
    assert _state("plugin:claude-sessions:telegram-1") == "pending"


def test_leaves_webui_events_with_no_ui_owner(monkeypatch):
    # Without origin_ui_session_id _process_one cannot place the event, so the row
    # must stay pending rather than be claimed and dropped.
    evt = _webui_event("plugin:claude-sessions:orphan-1", origin_ui_session_id="")
    _insert_pending("plugin:claude-sessions:orphan-1", evt)

    monkeypatch.setattr(bp, "_process_one", lambda e: pytest.fail("must not deliver"))

    assert bp.recover_durable_webui_events() == 0
    assert _state("plugin:claude-sessions:orphan-1") == "pending"


def test_skips_async_delegation_rows(monkeypatch):
    # async_delegation has its own claim contract and delivery path.
    evt = _webui_event("plugin:claude-sessions:async-1", type="async_delegation")
    _insert_pending("plugin:claude-sessions:async-1", evt)

    monkeypatch.setattr(bp, "_process_one", lambda e: pytest.fail("must not deliver"))

    assert bp.recover_durable_webui_events() == 0
    assert _state("plugin:claude-sessions:async-1") == "pending"


def test_delivery_failure_releases_the_claim(monkeypatch):
    evt = _webui_event("plugin:claude-sessions:boom-1")
    _insert_pending("plugin:claude-sessions:boom-1", evt)

    def explode(_e):
        raise RuntimeError("stream emit failed")

    monkeypatch.setattr(bp, "_process_one", explode)

    assert bp.recover_durable_webui_events() == 0
    # Still pending, so a later start can retry rather than losing the notice.
    assert _state("plugin:claude-sessions:boom-1") == "pending"


def test_missing_ledger_is_not_fatal(monkeypatch):
    # Cut-down vendoring / an older core without the durable ledger must no-op
    # rather than keep the drain thread from starting.
    import sqlite3

    def explode(*_a, **_k):
        raise sqlite3.OperationalError("no such table: background_events")

    monkeypatch.setattr(sqlite3, "connect", explode)
    assert bp.recover_durable_webui_events() == 0


def test_start_drain_thread_runs_durable_recovery(monkeypatch):
    calls = []

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def is_alive(self):
            return self.started

        def start(self):
            self.started = True

    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: 0)
    monkeypatch.setattr(
        bp, "recover_durable_webui_events", lambda: calls.append("durable") or 0
    )
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert calls == ["durable"]


def test_start_drain_thread_survives_durable_recovery_failure(monkeypatch):
    class _FakeThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def is_alive(self):
            return self.started

        def start(self):
            self.started = True

    def fail():
        raise OSError("ledger unreadable")

    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: 0)
    monkeypatch.setattr(bp, "recover_durable_webui_events", fail)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert bp._DRAIN_THREAD is not None
    assert bp._DRAIN_THREAD.is_alive()
