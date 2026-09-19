"""Access records must reach the process output, even during agent capture."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("capture", ["redirected", "closed"])
def test_successful_chat_start_delivers_access_record(tmp_path, capture):
    # A subprocess tests the delivered fd text, not an internal counter or a
    # patched print. Keep the real chat route and HTTP response/logging stack;
    # replace only model discovery and execution with deterministic fixtures.
    script = textwrap.dedent('''
        import io
        import json
        import sys
        import threading
        import time
        from unittest.mock import patch
        from server import Handler
        from api import routes
        from api.models import Session

        handler = Handler.__new__(Handler)
        handler.command = "POST"
        handler.path = "/api/chat/start"
        handler.requestline = "POST /api/chat/start HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler._req_t0 = time.time()
        session = Session(session_id="log-delivery", model="fixture")
        original = sys.stdout
        captured = io.StringIO()
        if sys.argv[1] == "closed":
            captured.close()

        def start_run(session, **kwargs):
            # Agent tool capture can redirect stdout from another thread before
            # chat/start writes its acceptance response.
            def capture_output():
                sys.stdout = captured
            worker = threading.Thread(target=capture_output)
            worker.start()
            worker.join()
            return {"stream_id": "log-stream", "session_id": session.session_id}

        try:
            with (
                patch.object(routes, "_agent_runtime_barrier_response", return_value=None),
                patch.object(routes, "_get_or_materialize_session", return_value=session),
                patch.object(routes, "_resolve_chat_workspace_with_recovery", return_value="/tmp"),
                patch.object(routes, "_read_profile_model_config", return_value=(None, "fixture", {})),
                patch.object(routes, "get_config", return_value={}),
                patch.object(routes, "_resolve_compatible_session_model_state", return_value=("fixture", None, False)),
                patch.object(routes, "_start_run", side_effect=start_run),
            ):
                routes._handle_chat_start(handler, {"session_id": session.session_id, "message": "wake"})
        finally:
            sys.stdout = original
        response = handler.wfile.getvalue()
        assert b"HTTP/1.1 200" in response, response
        assert json.loads(response.split(b"\\r\\n\\r\\n", 1)[1])["stream_id"] == "log-stream"
    ''')
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "home"),
        "HERMES_WEBUI_STATE_DIR": str(tmp_path / "state"),
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script, capture],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line.removeprefix("[webui] "))
        for line in result.stdout.splitlines() if line.startswith("[webui] ")
    ]
    assert len(records) == 1, f"Successful chat/start lost its access record: {result.stdout!r}"
    assert records[0]["method"] == "POST"
    assert records[0]["path"] == "/api/chat/start"
    assert records[0]["status"] == 200
    assert records[0]["ms"] >= 0
