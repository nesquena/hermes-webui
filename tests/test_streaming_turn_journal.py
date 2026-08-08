from __future__ import annotations

import threading
from unittest.mock import MagicMock


def test_local_worker_rejected_acceptance_never_reaches_provider_execution(monkeypatch):
    from api import streaming
    from api.config import STREAMS, STREAMS_LOCK

    stream_id = "local-journal-rejected"
    queue = MagicMock()
    acceptance_gate = threading.Event()
    acceptance_state = {"accepted": False}
    acceptance_gate.set()

    with STREAMS_LOCK:
        STREAMS[stream_id] = queue

    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(
            AssertionError("rejected local worker reached provider execution")
        ),
    )

    streaming._run_agent_streaming(
        session_id="local-journal-session",
        msg_text="wake up",
        model="test-model",
        workspace="/tmp/workspace",
        stream_id=stream_id,
        acceptance_gate=acceptance_gate,
        acceptance_state=acceptance_state,
    )

    with STREAMS_LOCK:
        assert stream_id not in STREAMS
